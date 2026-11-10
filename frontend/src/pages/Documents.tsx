import { useRef, useState, type DragEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { useAuth } from "../auth";

const ACCEPT = ".pdf,.docx,.xlsx,.csv,.html,.htm,.md,.markdown,.txt,.png,.jpg,.jpeg,.webp";

export default function Documents() {
  const { user } = useAuth();
  const qc = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const [notes, setNotes] = useState<string[]>([]);
  const [over, setOver] = useState(false);
  const canUpload = user?.role === "admin" || user?.role === "ingestor";
  const isAdmin = user?.role === "admin";

  const list = useQuery({
    queryKey: ["documents"],
    queryFn: api.documents,
    // Poll while something is still being processed.
    refetchInterval: (q) =>
      q.state.data?.some((d) => d.status === "pending" || d.status === "processing") ? 3000 : false,
  });

  const upload = useMutation({
    mutationFn: api.upload,
    onSuccess: (r, file) => {
      const msg =
        r.status === "duplicate"
          ? `${file.name}: identical content is already ingested, skipped.`
          : `${file.name}: uploaded as version ${r.version}, processing.`;
      setNotes((n) => [msg, ...n].slice(0, 5));
      void qc.invalidateQueries({ queryKey: ["documents"] });
    },
    onError: (e, file) =>
      setNotes((n) => [`${file.name}: ${e instanceof Error ? e.message : "upload failed"}`, ...n].slice(0, 5)),
  });

  const remove = useMutation({
    mutationFn: api.deleteDocument,
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["documents"] }),
  });

  async function addFiles(files: FileList | File[]) {
    for (const f of Array.from(files)) await upload.mutateAsync(f).catch(() => undefined); // one at a time
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    setOver(false);
    if (canUpload) void addFiles(e.dataTransfer.files);
  }

  return (
    <div className="page">
      <h2>Documents</h2>
      {canUpload ? (
        <div
          className={`drop ${over ? "over" : ""}`}
          onDragOver={(e) => {
            e.preventDefault();
            setOver(true);
          }}
          onDragLeave={() => setOver(false)}
          onDrop={onDrop}
        >
          <p>Drop files here, or</p>
          <button onClick={() => input.current?.click()} disabled={upload.isPending}>
            {upload.isPending ? "Uploading…" : "Choose files"}
          </button>
          <input
            ref={input}
            type="file"
            multiple
            accept={ACCEPT}
            hidden
            onChange={(e) => {
              if (e.target.files) void addFiles(e.target.files);
              e.target.value = "";
            }}
          />
          <p className="muted small">PDF, DOCX, XLSX, CSV, HTML, Markdown, TXT, PNG, JPG, WEBP · up to 20 MB each</p>
        </div>
      ) : (
        <p className="muted">Your role can search documents but not upload them.</p>
      )}
      {notes.length > 0 && (
        <ul className="notes" aria-live="polite">
          {notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ul>
      )}

      {list.isLoading && <p className="muted">Loading…</p>}
      {list.error && <p className="error">{(list.error as Error).message}</p>}
      {list.data && list.data.length === 0 && <p className="muted">No documents yet.</p>}
      {list.data && list.data.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>File</th>
              <th>Version</th>
              <th>Status</th>
              <th>Chunks</th>
              <th>Uploaded</th>
              {isAdmin && <th />}
            </tr>
          </thead>
          <tbody>
            {list.data.map((d) => (
              <tr key={d.document_id}>
                <td>{d.filename}</td>
                <td>v{d.version}</td>
                <td>
                  <span className={`badge ${d.status}`}>{d.status}</span>
                  {d.error && <div className="error small">{d.error}</div>}
                </td>
                <td>{d.chunk_count}</td>
                <td>{d.created_at}</td>
                {isAdmin && (
                  <td>
                    <button
                      className="ghost danger"
                      disabled={remove.isPending}
                      onClick={() => {
                        if (confirm(`Delete ${d.filename} and all its versions? This cannot be undone.`))
                          remove.mutate(d.document_id);
                      }}
                    >
                      Delete
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
