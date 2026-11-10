export type Role = "admin" | "ingestor" | "user";

export interface Me {
  username: string;
  role: Role;
}

export interface Citation {
  tag: string;
  document_id: string | null;
  filename: string | null;
  page: number | null;
  section: string | null;
  type: string | null;
}

export interface ImageRef {
  tag: string;
  url: string;
  page: number | null;
  caption: string;
}

export interface FinalEvent {
  type: "final";
  answer: string;
  found: boolean;
  citations: Citation[];
  images: ImageRef[];
  trace_id: string | null; // set when tracing is on; needed to send feedback
}

export type ChatEvent =
  | { type: "token"; text: string }
  | { type: "replace"; text: string }
  | FinalEvent
  | { type: "error"; message: string };

export interface DocRow {
  document_id: string;
  filename: string;
  version: number;
  status: "pending" | "processing" | "ready" | "failed" | "superseded";
  chunk_count: number;
  error: string | null;
  created_at: string;
}

export interface UploadResult {
  status: string;
  document_id: string;
  version: number;
}
