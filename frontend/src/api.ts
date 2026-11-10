import type { ChatEvent, DocRow, Me, Role, UploadResult } from "./types";

// The token lives in sessionStorage (cleared when the tab closes), not localStorage.
// Any script injected into the page could still read it, which is why the UI renders
// model output as Markdown without raw HTML.
const KEY = "erag_token";
let token: string | null = null;
try {
  token = sessionStorage.getItem(KEY);
} catch {
  token = null;
}
let onUnauthorized: () => void = () => {};

export function setToken(t: string | null) {
  token = t;
  try {
    if (t) sessionStorage.setItem(KEY, t);
    else sessionStorage.removeItem(KEY);
  } catch {
    /* storage unavailable: token stays in memory only */
  }
}
export const hasToken = () => token !== null;
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function errorFrom(res: Response): Promise<ApiError> {
  let msg = `${res.status} ${res.statusText}`;
  try {
    const body = await res.json();
    const d = body.detail;
    if (typeof d === "string") msg = d;
    else if (Array.isArray(d)) msg = d.map((e) => e.msg ?? JSON.stringify(e)).join("; ");
  } catch {
    /* non-JSON error body */
  }
  return new ApiError(res.status, msg);
}

async function request<T>(path: string, init: RequestInit = {}, auth = true): Promise<T> {
  const headers = new Headers(init.headers);
  if (auth && token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(path, { ...init, headers });
  if (res.status === 401 && auth) onUnauthorized();
  if (!res.ok) throw await errorFrom(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  async login(username: string, password: string): Promise<void> {
    const body = new URLSearchParams({ username, password });
    const r = await request<{ access_token: string }>("/auth/login", { method: "POST", body }, false);
    setToken(r.access_token);
  },
  me: () => request<Me>("/auth/me"),
  documents: () => request<DocRow[]>("/documents"),
  upload(file: File) {
    const body = new FormData();
    body.append("file", file);
    return request<UploadResult>("/documents", { method: "POST", body });
  },
  feedback: (trace_id: string, value: "up" | "down", comment?: string) =>
    request<void>("/chat/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ trace_id, value, comment }),
    }),
  deleteDocument: (id: string) => request<void>(`/documents/${id}`, { method: "DELETE" }),
  createUser: (username: string, password: string, role: Role) =>
    request<{ username: string; role: Role }>("/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password, role }),
    }),
};

export interface ChatBody {
  question: string;
  document_id?: string;
  type?: "text" | "table" | "image";
}

/** POST /chat/stream and call onEvent for each server-sent event. */
export async function streamChat(
  body: ChatBody,
  onEvent: (ev: ChatEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const res = await fetch("/chat/stream", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body),
    signal,
  });
  if (res.status === 401) onUnauthorized();
  if (!res.ok || !res.body) throw await errorFrom(res);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const data = block
        .split("\n")
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).trimStart())
        .join("\n");
      if (data) onEvent(JSON.parse(data) as ChatEvent);
    }
  }
}
