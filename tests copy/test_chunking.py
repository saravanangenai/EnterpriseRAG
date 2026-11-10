from app.ingestion.chunking import Chunk, chunk_blocks
from app.ingestion.parsers import Block


def test_long_text_split_with_overlap():
    text = ". ".join(f"Sentence number {i} about refunds" for i in range(200))
    chunks = chunk_blocks([Block("text", text, page=3, section="Refunds")], 400, 15)
    assert len(chunks) > 5
    assert all(len(c.text) <= 400 for c in chunks)
    assert all(c.page == 3 and c.section == "Refunds" for c in chunks)


def test_small_table_kept_whole():
    md = "| a | b |\n|---|---|\n| 1 | 2 |"
    chunks = chunk_blocks([Block("table", md)], 400, 15)
    assert len(chunks) == 1 and chunks[0].type == "table"


def test_wide_table_split_keeps_header():
    rows = "\n".join(f"| row{i} | {'x' * 50} |" for i in range(60))
    md = "| a | b |\n|---|---|\n" + rows
    chunks = chunk_blocks([Block("table", md)], 300, 15)
    assert len(chunks) > 1
    assert all(c.text.startswith("| a | b |") for c in chunks)


def test_image_caption_chunk_keeps_asset_path():
    chunks = chunk_blocks([Block("image", "A bar chart", page=2, asset_path="assets/d/v1/p2.png")], 400, 15)
    assert chunks[0].type == "image" and chunks[0].asset_path == "assets/d/v1/p2.png"


def test_point_id_is_content_based():
    a, b = Chunk("same text", "text", page=1), Chunk("same text", "text", page=9)
    assert a.point_id("doc") == b.point_id("doc")  # page change does not change the ID
    assert a.point_id("doc") != Chunk("other", "text").point_id("doc")
    assert a.point_id("doc") != a.point_id("doc2")
