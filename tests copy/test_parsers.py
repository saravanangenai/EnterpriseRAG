import io

from PIL import Image

from app.ingestion.parsers import ParseContext, parse_file


def ctx(tmp_path):
    return ParseContext(data_dir=tmp_path, assets_dir=tmp_path / "assets" / "d" / "v1")


def png(size=(200, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()


def test_markdown_sections(tmp_path):
    f = tmp_path / "a.md"
    f.write_text("# Intro\nhello world\n\n# Terms\npayment in 30 days\n")
    blocks = parse_file(f, ctx(tmp_path))
    assert [b.section for b in blocks] == ["Intro", "Terms"]
    assert "payment in 30 days" in blocks[1].text


def test_csv_becomes_table_with_header_repeated(tmp_path):
    f = tmp_path / "t.csv"
    rows = ["name,amount"] + [f"item{i},{i}" for i in range(95)]
    f.write_text("\n".join(rows))
    blocks = parse_file(f, ctx(tmp_path))
    assert len(blocks) == 3  # 95 rows -> groups of 40
    assert all(b.kind == "table" and b.text.startswith("| name | amount |") for b in blocks)


def test_xlsx_sheets(tmp_path):
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sales"
    ws.append(["region", "total"])
    ws.append(["west", 100])
    wb.save(tmp_path / "s.xlsx")
    blocks = parse_file(tmp_path / "s.xlsx", ctx(tmp_path))
    assert blocks[0].section == "Sales" and "west" in blocks[0].text


def test_html_strips_boilerplate(tmp_path):
    f = tmp_path / "p.html"
    f.write_text(
        "<html><body><nav>MENU</nav><h1>Policy</h1><p>Refunds in 14 days.</p>"
        "<script>evil()</script><table><tr><th>a</th></tr><tr><td>1</td></tr></table></body></html>"
    )
    blocks = parse_file(f, ctx(tmp_path))
    text = " ".join(b.text for b in blocks)
    assert "MENU" not in text and "evil" not in text
    assert "Refunds in 14 days." in text
    assert any(b.kind == "table" for b in blocks)


def test_docx_headings_tables_images(tmp_path):
    import docx

    d = docx.Document()
    d.add_heading("Coverage", level=1)
    d.add_paragraph("The plan covers fire damage.")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "item", "limit"
    t.cell(1, 0).text, t.cell(1, 1).text = "fire", "500"
    (tmp_path / "pic.png").write_bytes(png())
    d.add_picture(str(tmp_path / "pic.png"))
    d.save(tmp_path / "d.docx")
    blocks = parse_file(tmp_path / "d.docx", ctx(tmp_path))
    kinds = [b.kind for b in blocks]
    assert "text" in kinds and "table" in kinds and "image" in kinds
    assert blocks[0].section == "Coverage"
    img = next(b for b in blocks if b.kind == "image")
    assert (tmp_path / img.asset_path).exists()


def test_pdf_text_page_and_scanned_page(tmp_path):
    import pymupdf

    doc = pymupdf.open()
    p1 = doc.new_page()
    p1.insert_text((72, 72), "Quarterly revenue grew twelve percent over last year.")
    doc.new_page()  # blank page behaves like a scanned page
    doc.save(tmp_path / "r.pdf")
    blocks = parse_file(tmp_path / "r.pdf", ctx(tmp_path))
    assert blocks[0].kind == "text" and blocks[0].page == 1
    scanned = [b for b in blocks if b.page == 2]
    assert scanned and scanned[0].kind == "image"
    assert (tmp_path / scanned[0].asset_path).exists()


def test_standalone_image(tmp_path):
    (tmp_path / "x.png").write_bytes(png())
    blocks = parse_file(tmp_path / "x.png", ctx(tmp_path))
    assert blocks[0].kind == "image" and blocks[0].mime == "image/png"
