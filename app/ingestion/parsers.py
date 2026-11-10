"""One parser per file type. All yield the same Block format.

Images are written to the assets folder as soon as they are found, so Blocks only
carry a path (keeps memory low on a 1 GB VM).
"""
import csv
import io
import mimetypes
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

MIN_PAGE_TEXT = 25  # fewer characters than this on a PDF page => treat as scanned
TABLE_ROWS_PER_BLOCK = 40


@dataclass
class Block:
    kind: str  # text | table | image
    text: str = ""
    page: int | None = None
    section: str | None = None
    asset_path: str | None = None  # relative to data dir, posix style
    mime: str | None = None


@dataclass
class ParseContext:
    data_dir: Path
    assets_dir: Path  # data/assets/<document_id>/v<version>
    image_min_px: int = 100
    pdf_render_dpi: int = 120

    def save_image(self, data: bytes, name: str) -> str:
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        path = self.assets_dir / name
        path.write_bytes(data)
        return path.relative_to(self.data_dir).as_posix()


def _is_big_enough(data: bytes, min_px: int) -> bool:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:
            return im.width >= min_px and im.height >= min_px
    except Exception:
        return False


def rows_to_markdown(rows: list[list]) -> str:
    cleaned = [
        [("" if c is None else str(c)).replace("\n", " ").replace("|", "/").strip() for c in r]
        for r in rows
    ]
    cleaned = [r for r in cleaned if any(r)]
    if not cleaned:
        return ""
    width = max(len(r) for r in cleaned)
    cleaned = [r + [""] * (width - len(r)) for r in cleaned]
    lines = ["| " + " | ".join(cleaned[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in cleaned[1:]]
    return "\n".join(lines)


def table_blocks(rows: list[list], section: str | None, page: int | None = None) -> Iterator[Block]:
    """Split a long table into row groups, repeating the header row in each."""
    rows = [r for r in rows if any(c not in (None, "") for c in r)]
    if not rows:
        return
    header, body = rows[0], rows[1:]
    if not body:
        md = rows_to_markdown([header])
        if md:
            yield Block("table", md, page, section)
        return
    for i in range(0, len(body), TABLE_ROWS_PER_BLOCK):
        md = rows_to_markdown([header] + body[i : i + TABLE_ROWS_PER_BLOCK])
        if md:
            yield Block("table", md, page, section)


# ---------------------------------------------------------------- PDF
def parse_pdf(path: Path, ctx: ParseContext) -> Iterator[Block]:
    import pdfplumber
    import pymupdf

    doc = pymupdf.open(path)
    try:
        with pdfplumber.open(path) as plumber:
            for i, page in enumerate(doc, start=1):
                text = page.get_text("text").strip()
                scanned = len(text) < MIN_PAGE_TEXT
                if not scanned:
                    yield Block("text", text, page=i)
                    try:
                        tables = plumber.pages[i - 1].extract_tables()
                    except Exception:
                        tables = []
                    for t in tables:
                        yield from table_blocks(t, None, i)
                    plumber.pages[i - 1].flush_cache()
                    for n, info in enumerate(page.get_images(full=True)):
                        try:
                            pix = pymupdf.Pixmap(doc, info[0])
                            if pix.width < ctx.image_min_px or pix.height < ctx.image_min_px:
                                continue
                            if pix.n - pix.alpha >= 4:
                                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                            rel = ctx.save_image(pix.tobytes("png"), f"p{i}_img{n}.png")
                            yield Block("image", page=i, asset_path=rel, mime="image/png")
                        except Exception:
                            continue
                else:
                    pix = page.get_pixmap(dpi=ctx.pdf_render_dpi)
                    rel = ctx.save_image(pix.tobytes("png"), f"p{i}_page.png")
                    yield Block("image", page=i, asset_path=rel, mime="image/png")
    finally:
        doc.close()


# ---------------------------------------------------------------- DOCX
def parse_docx(path: Path, ctx: ParseContext) -> Iterator[Block]:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(path)
    section: str | None = None
    buf: list[str] = []

    def flush() -> Iterator[Block]:
        if buf:
            yield Block("text", "\n".join(buf), section=section)
            buf.clear()

    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, d)
            txt = p.text.strip()
            if not txt:
                continue
            if (p.style.name or "").lower().startswith("heading"):
                yield from flush()
                section = txt
            buf.append(txt)
        elif tag == "tbl":
            yield from flush()
            rows = [[c.text for c in r.cells] for r in Table(child, d).rows]
            yield from table_blocks(rows, section)
    yield from flush()

    for n, rel in enumerate(d.part.rels.values()):
        if "image" in rel.reltype:
            blob = rel.target_part.blob
            if _is_big_enough(blob, ctx.image_min_px):
                ext = mimetypes.guess_extension(rel.target_part.content_type) or ".png"
                yield Block(
                    "image",
                    asset_path=ctx.save_image(blob, f"img{n}{ext}"),
                    mime=rel.target_part.content_type,
                )


# ---------------------------------------------------------------- XLSX / CSV
def parse_xlsx(path: Path, ctx: ParseContext) -> Iterator[Block]:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            yield from table_blocks(rows, ws.title)
    finally:
        wb.close()


def parse_csv(path: Path, ctx: ParseContext) -> Iterator[Block]:
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        rows = list(csv.reader(f))
    yield from table_blocks(rows, path.stem)


# ---------------------------------------------------------------- HTML / MD / TXT
def parse_html(path: Path, ctx: ParseContext) -> Iterator[Block]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
        tag.decompose()
    section: str | None = None
    buf: list[str] = []

    def flush() -> Iterator[Block]:
        if buf:
            yield Block("text", "\n".join(buf), section=section)
            buf.clear()

    for el in (soup.body or soup).find_all(
        ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "table"]
    ):
        if el.name != "table" and el.find_parent("table"):
            continue
        if el.name == "table":
            yield from flush()
            rows = [
                [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                for tr in el.find_all("tr")
            ]
            yield from table_blocks(rows, section)
            continue
        txt = el.get_text(" ", strip=True)
        if not txt:
            continue
        if el.name.startswith("h"):
            yield from flush()
            section = txt
        buf.append(txt)
    yield from flush()


def parse_markdown(path: Path, ctx: ParseContext) -> Iterator[Block]:
    section: str | None = None
    buf: list[str] = []

    def flush() -> Iterator[Block]:
        text = "\n".join(buf).strip()
        if text:
            yield Block("text", text, section=section)
        buf.clear()

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lstrip().startswith("#"):
            yield from flush()
            section = line.lstrip("# ").strip()
        buf.append(line)
    yield from flush()


def parse_text(path: Path, ctx: ParseContext) -> Iterator[Block]:
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if text:
        yield Block("text", text)


# ---------------------------------------------------------------- images
def parse_image(path: Path, ctx: ParseContext) -> Iterator[Block]:
    data = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    yield Block("image", asset_path=ctx.save_image(data, f"image{path.suffix.lower()}"), mime=mime)


PARSERS = {
    ".pdf": parse_pdf,
    ".docx": parse_docx,
    ".xlsx": parse_xlsx,
    ".csv": parse_csv,
    ".html": parse_html,
    ".htm": parse_html,
    ".md": parse_markdown,
    ".markdown": parse_markdown,
    ".txt": parse_text,
    ".png": parse_image,
    ".jpg": parse_image,
    ".jpeg": parse_image,
    ".webp": parse_image,
}


def parse_file(path: Path, ctx: ParseContext) -> list[Block]:
    parser = PARSERS.get(path.suffix.lower())
    if parser is None:
        raise ValueError(f"Unsupported file type: {path.suffix}")
    return list(parser(path, ctx))
