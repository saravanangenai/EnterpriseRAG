import hashlib
import uuid
from dataclasses import dataclass

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.ingestion.parsers import Block

_NS = uuid.UUID("6f1c2f0e-5d6a-4b57-9f0b-1f3d3a9c2e11")


@dataclass
class Chunk:
    text: str
    type: str  # text | table | image
    page: int | None = None
    section: str | None = None
    asset_path: str | None = None
    pii_flags: list[str] | None = None

    @property
    def sha256(self) -> str:
        key = f"{self.type}\x00{self.asset_path or ''}\x00{self.text}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def point_id(self, document_id: str) -> str:
        """Content-based ID: an unchanged chunk keeps its ID, so it is never re-embedded."""
        return str(uuid.uuid5(_NS, f"{document_id}:{self.sha256}"))


def chunk_blocks(blocks: list[Block], size: int, overlap_pct: int) -> list[Chunk]:
    overlap = max(0, int(size * overlap_pct / 100))
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=size, chunk_overlap=overlap, separators=["\n\n", "\n", ". ", " ", ""]
    )
    out: list[Chunk] = []
    for b in blocks:
        text = b.text.strip()
        if not text:
            continue
        if b.kind == "text":
            for piece in splitter.split_text(text):
                out.append(Chunk(piece, "text", b.page, b.section))
        elif b.kind == "table":
            if len(text) <= size * 2:
                out.append(Chunk(text, "table", b.page, b.section))
            else:  # very wide table: split by lines, keep the header line
                lines = text.split("\n")
                head, body = lines[:2], lines[2:]
                cur: list[str] = []
                for line in body:
                    if cur and sum(len(x) + 1 for x in head + cur) + len(line) > size * 2:
                        out.append(Chunk("\n".join(head + cur), "table", b.page, b.section))
                        cur = []
                    cur.append(line)
                if cur:
                    out.append(Chunk("\n".join(head + cur), "table", b.page, b.section))
        elif b.kind == "image":
            out.append(Chunk(text, "image", b.page, b.section, b.asset_path))
    return out
