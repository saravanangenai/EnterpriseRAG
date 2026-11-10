import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import { api, streamChat } from "../api";
import type { Citation, ImageRef } from "../types";

interface Msg {
  id: number;
  role: "user" | "assistant";
  text: string;
  citations?: Citation[];
  images?: ImageRef[];
  found?: boolean;
  streaming?: boolean;
  error?: string;
  traceId?: string | null;
  rated?: "up" | "down";
}

let nextId = 1;

function CitationList({ items }: { items: Citation[] }) {
  return (
    <ul className="chips">
      {items.map((c) => (
        <li key={c.tag} className="chip" title={c.section ?? undefined}>
          <b>{c.tag}</b> {c.filename ?? "unknown"}
          {c.page ? ` · p.${c.page}` : ""}
          {c.type && c.type !== "text" ? ` · ${c.type}` : ""}
        </li>
      ))}
    </ul>
  );
}

function Images({ items }: { items: ImageRef[] }) {
  return (
    <div className="images">
      {items.map((im) => (
        <figure key={im.url}>
          <a href={im.url} target="_blank" rel="noreferrer">
            <img src={im.url} alt={im.caption || `Source ${im.tag}`} loading="lazy" />
          </a>
          <figcaption>
            <b>{im.tag}</b>
            {im.page ? ` · p.${im.page}` : ""}
          </figcaption>
        </figure>
      ))}
    </div>
  );
}

export default function Chat() {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [docId, setDocId] = useState("");
  const [type, setType] = useState("");
  const [busy, setBusy] = useState(false);
  const abort = useRef<AbortController | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const docs = useQuery({ queryKey: ["documents"], queryFn: api.documents });

  // Effect callbacks must return nothing or a cleanup function, so use block bodies.
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [msgs]);
  useEffect(() => {
    return () => abort.current?.abort();
  }, []);

  const patch = (id: number, fn: (m: Msg) => Msg) =>
    setMsgs((all) => all.map((m) => (m.id === id ? fn(m) : m)));

  async function send() {
    const question = input.trim();
    if (!question || busy) return;
    const aId = nextId++;
    setMsgs((all) => [
      ...all,
      { id: nextId++, role: "user", text: question },
      { id: aId, role: "assistant", text: "", streaming: true },
    ]);
    setInput("");
    setBusy(true);
    const ctl = new AbortController();
    abort.current = ctl;
    try {
      await streamChat(
        {
          question,
          document_id: docId || undefined,
          type: (type || undefined) as "text" | "table" | "image" | undefined,
        },
        (ev) => {
          if (ev.type === "token") patch(aId, (m) => ({ ...m, text: m.text + ev.text }));
          else if (ev.type === "replace") patch(aId, (m) => ({ ...m, text: ev.text }));
          else if (ev.type === "final")
            patch(aId, (m) => ({
              ...m,
              text: ev.answer,
              citations: ev.citations,
              images: ev.images,
              found: ev.found,
              traceId: ev.trace_id,
              streaming: false,
            }));
          else if (ev.type === "error")
            patch(aId, (m) => ({ ...m, error: "The answer could not be completed.", streaming: false }));
        },
        ctl.signal,
      );
    } catch (err) {
      if (!ctl.signal.aborted)
        patch(aId, (m) => ({
          ...m,
          error: err instanceof Error ? err.message : "Request failed",
          streaming: false,
        }));
    } finally {
      patch(aId, (m) => ({ ...m, streaming: false }));
      setBusy(false);
      abort.current = null;
    }
  }

  async function rate(m: Msg, value: "up" | "down") {
    if (!m.traceId || m.rated) return;
    patch(m.id, (x) => ({ ...x, rated: value })); // optimistic; undone if the call fails
    try {
      await api.feedback(m.traceId, value);
    } catch {
      patch(m.id, (x) => ({ ...x, rated: undefined }));
    }
  }

  function onKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  }

  const ready = (docs.data ?? []).filter((d) => d.status === "ready");

  return (
    <div className="chat">
      <div className="log" aria-live="polite">
        {msgs.length === 0 && (
          <p className="muted center-text">
            Ask a question about the uploaded documents. Answers cite their sources and show the
            original figures when they were used.
          </p>
        )}
        {msgs.map((m) => (
          <div key={m.id} className={`msg ${m.role}`}>
            {m.role === "user" ? (
              <p>{m.text}</p>
            ) : (
              <>
                {m.text ? (
                  <div className={m.found === false ? "answer notfound" : "answer"}>
                    <ReactMarkdown>{m.text}</ReactMarkdown>
                  </div>
                ) : (
                  m.streaming && <p className="muted">Thinking…</p>
                )}
                {m.error && <p className="error">{m.error}</p>}
                {m.citations && m.citations.length > 0 && <CitationList items={m.citations} />}
                {m.images && m.images.length > 0 && <Images items={m.images} />}
                {m.traceId && !m.streaming && (
                  <div className="rate" role="group" aria-label="Rate this answer">
                    <button
                      className={`ghost ${m.rated === "up" ? "on" : ""}`}
                      disabled={!!m.rated}
                      onClick={() => void rate(m, "up")}
                      aria-label="Good answer"
                    >
                      Helpful
                    </button>
                    <button
                      className={`ghost ${m.rated === "down" ? "on" : ""}`}
                      disabled={!!m.rated}
                      onClick={() => void rate(m, "down")}
                      aria-label="Bad answer"
                    >
                      Not helpful
                    </button>
                  </div>
                )}
              </>
            )}
          </div>
        ))}
        <div ref={bottom} />
      </div>

      <div className="composer">
        <div className="filters">
          <select value={docId} onChange={(e) => setDocId(e.target.value)} aria-label="Document filter">
            <option value="">All documents</option>
            {ready.map((d) => (
              <option key={d.document_id} value={d.document_id}>
                {d.filename}
              </option>
            ))}
          </select>
          <select value={type} onChange={(e) => setType(e.target.value)} aria-label="Content type filter">
            <option value="">Any content</option>
            <option value="text">Text</option>
            <option value="table">Tables</option>
            <option value="image">Images and charts</option>
          </select>
        </div>
        <div className="row">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKey}
            placeholder="Ask a question (Enter to send, Shift+Enter for a new line)"
            rows={2}
            maxLength={2000}
          />
          {busy ? (
            <button className="ghost" onClick={() => abort.current?.abort()}>
              Stop
            </button>
          ) : (
            <button onClick={() => void send()} disabled={!input.trim()}>
              Send
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
