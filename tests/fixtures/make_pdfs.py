"""Make the PDFs under tests/fixtures/pdf/ (D48: page counts from real producers).

Run from a checkout on macOS (cupsfilter and sips are the system's; the rest come from PyPI):

    uv run --no-project --with reportlab --with fpdf2 --with pikepdf --with pymupdf python tests/fixtures/make_pdfs.py

Every file holds only synthetic text. ``EXPECTED`` is each file's true page count, which
tests/test_image_estimate.py asserts; the PDFlib-shaped and incremental-update files are written by
hand, since PDFlib is commercial and the libraries above rewrite a file whole.
"""

from __future__ import annotations

import io
import re
import struct
import subprocess
import tempfile
import zlib
from datetime import UTC, datetime
from pathlib import Path

OUT = Path(__file__).parent / "pdf"
EXPECTED = {
    "reportlab-3.pdf": 3,
    "fpdf2-5.pdf": 5,
    "pikepdf-objstm-12.pdf": 12,
    "pikepdf-objstm-linearized-12.pdf": 12,
    "pymupdf-objstm-stored-9.pdf": 9,
    "cupsfilter-text.pdf": 2,
    "sips-image-1.pdf": 1,
    "incremental-added-page-4.pdf": 4,
    "incremental-edited-page-3.pdf": 3,
    "pdflib-indirect-length-1.pdf": 1,
    "pdflib-indirect-length-7.pdf": 7,
}
# Files whose page count runs high on purpose (true pages here): an incremental save that deleted pages
# leaves the superseded page tree listing them (D48 review round 4).
OVERCOUNTED = {
    "pymupdf-incremental-deleted-40-to-10.pdf": 10,
}


def reportlab(pages: int) -> bytes:
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, invariant=1)  # invariant: no dates or ids, so the bytes are reproducible
    for page in range(pages):
        pdf.drawString(72, 720, f"Synthetic page {page + 1} of {pages}.")
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def fpdf2(pages: int) -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_creation_date(datetime(2026, 1, 1, tzinfo=UTC))  # a fixed date, so the bytes are reproducible
    pdf.set_font("helvetica", size=12)
    for page in range(pages):
        pdf.add_page()
        pdf.cell(text=f"Synthetic page {page + 1} of {pages}.")
    return bytes(pdf.output())


def pikepdf_objstm(pages: int, *, linearize: bool) -> bytes:
    import pikepdf

    pdf = pikepdf.open(io.BytesIO(reportlab(pages)))
    buffer = io.BytesIO()
    pdf.save(buffer, object_stream_mode=pikepdf.ObjectStreamMode.generate, linearize=linearize,
             deterministic_id=True)
    return buffer.getvalue()


def pymupdf_objstm(pages: int) -> bytes:
    """PyMuPDF stores its object streams without a filter (D48 review round 3)."""
    import pymupdf

    doc = pymupdf.open()
    for page in range(pages):
        doc.new_page().insert_text((72, 72), f"Synthetic page {page + 1} of {pages}.")
    doc.set_metadata({})
    return doc.tobytes(use_objstms=1, garbage=1, no_new_id=1)


def pymupdf_deleted(pages: int, keep: int) -> bytes:
    """``pages`` text pages saved, then all but the first ``keep`` deleted by an incremental save."""
    import pymupdf

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "synthetic.pdf"
        doc = pymupdf.open()
        for page in range(pages):
            doc.new_page().insert_text((72, 72), f"Synthetic page {page + 1} of {pages}.")
        doc.set_metadata({})
        doc.save(path, no_new_id=1)
        doc.close()
        doc = pymupdf.open(path)
        doc.delete_pages(range(keep, pages))
        doc.saveIncr()
        doc.close()
        return path.read_bytes()


def cupsfilter_text() -> bytes:
    with tempfile.TemporaryDirectory() as directory:
        text = Path(directory) / "synthetic.txt"
        text.write_text("".join(f"Synthetic line {i}.\n" for i in range(90)))  # 66 lines a page: 2 pages
        return subprocess.run(["cupsfilter", "-m", "application/pdf", str(text)], check=True,
                              capture_output=True).stdout


def sips_image() -> bytes:
    with tempfile.TemporaryDirectory() as directory:
        png, pdf = Path(directory) / "synthetic.png", Path(directory) / "synthetic.pdf"
        ihdr = struct.pack(">IIBBBBB", 64, 48, 8, 2, 0, 0, 0)
        raw = b"".join(b"\x00" + bytes(64 * 3) for _ in range(48))
        chunks = [(b"IHDR", ihdr), (b"IDAT", zlib.compress(raw)), (b"IEND", b"")]
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"".join(
            struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d)) for t, d in chunks))
        subprocess.run(["sips", "-s", "format", "pdf", str(png), "--out", str(pdf)], check=True, capture_output=True)
        return pdf.read_bytes()


def incremental(base: bytes, objects: dict[int, bytes], root: int) -> bytes:
    """``base`` with an incremental update: ``objects`` appended, a new xref section, and /Prev."""
    prev = int(re.findall(rb"startxref\s+(\d+)", base)[-1])
    size = int(re.findall(rb"/Size\s+(\d+)", base)[-1])
    body = bytearray(base if base.endswith(b"\n") else base + b"\n")
    offsets = {}
    for number, content in sorted(objects.items()):
        offsets[number] = len(body)
        body += b"%d 0 obj\n" % number + content + b"\nendobj\n"
    xref = len(body)
    body += b"xref\n"
    for number in sorted(offsets):
        body += b"%d 1\n%010d 00000 n \n" % (number, offsets[number])
    size = max(size, max(offsets) + 1)
    body += b"trailer\n<< /Size %d /Root %d 0 R /Prev %d >>\nstartxref\n%d\n%%%%EOF\n" % (size, root, prev, xref)
    return bytes(body)


def pages_object(base: bytes) -> tuple[int, int, list[int]]:
    """(catalog number, page tree number, page numbers) of a reportlab file."""
    catalog = int(re.search(rb"(\d+) 0 obj\s*<<[^>]*?/Type /Catalog", base).group(1))
    tree = int(re.search(rb"(\d+) 0 obj\s*<<\s*/Count", base).group(1))
    kids = [int(n) for n in re.findall(rb"(\d+) 0 R", re.search(rb"/Kids \[([^\]]*)\]", base).group(1))]
    return catalog, tree, kids


def added_page() -> bytes:
    base = reportlab(3)
    catalog, tree, kids = pages_object(base)
    size = int(re.findall(rb"/Size\s+(\d+)", base)[-1])
    new = size
    page = b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] /Contents %d 0 R /Resources << >> >>" % (
        tree, new + 1)
    content = b"<< /Length 0 >>\nstream\n\nendstream"
    kids_ref = b" ".join(b"%d 0 R" % k for k in [*kids, new])
    tree_obj = b"<< /Type /Pages /Count 4 /Kids [ %s ] >>" % kids_ref
    return incremental(base, {tree: tree_obj, new: page, new + 1: content}, catalog)


def edited_page() -> bytes:
    base = reportlab(3)
    catalog, tree, kids = pages_object(base)
    first = re.search(rb"%d 0 obj\s*(<<.*?>>)\s*endobj" % kids[0], base, re.S).group(1)
    return incremental(base, {kids[0]: first.replace(b"/Type /Page", b"/Rotate 90 /Type /Page")}, catalog)


def pdflib_like(pages: int) -> bytes:
    """The shape PDFlib+PDI 9 writes: the page tree and pages in an object stream whose /Length is an
    indirect object with a two-digit number (reviewer A, PR #234, round 2)."""
    page_numbers = list(range(3, 3 + pages))
    objects = [(2, b"<</Type/Pages/Count %d/Kids[%s]>>" % (pages, b" ".join(b"%d 0 R" % n for n in page_numbers)))]
    objects += [(n, b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>") for n in page_numbers]
    table, data = [], b""
    for number, body in objects:
        table.append(b"%d %d" % (number, len(data)))
        data += body + b" "
    header = b" ".join(table) + b" "
    packed = zlib.compress(header + data)
    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    offsets = {1: len(out)}
    out += b"1 0 obj\n<</Type/Catalog/Pages 2 0 R>>\nendobj\n"
    offsets[14] = len(out)
    out += b"14 0 obj\n<</Length 15 0 R/Filter/FlateDecode/Type/ObjStm/N %d/First %d>>\nstream\n" % (
        len(objects), len(header))
    out += packed + b"\nendstream\nendobj\n"
    offsets[15] = len(out)
    out += b"15 0 obj\n%d\nendobj\n" % len(packed)
    # A cross-reference stream, which objects inside an object stream need: type 1 at an offset, type 2
    # at an index in stream 14.
    rows = {0: (0, 0, 65535), 1: (1, offsets[1], 0), 14: (1, offsets[14], 0), 15: (1, offsets[15], 0)}
    rows.update({number: (2, 14, index) for index, (number, _) in enumerate(objects)})
    xref = len(out)
    rows[16] = (1, xref, 0)
    table = b"".join(struct.pack(">BIH", *rows.get(n, (0, 0, 0))) for n in range(17))
    out += b"16 0 obj\n<</Type/XRef/Size 17/W[1 4 2]/Root 1 0 R/Length %d>>\nstream\n" % len(table)
    out += table + b"\nendstream\nendobj\nstartxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    made = {
        "reportlab-3.pdf": reportlab(3),
        "fpdf2-5.pdf": fpdf2(5),
        "pikepdf-objstm-12.pdf": pikepdf_objstm(12, linearize=False),
        "pikepdf-objstm-linearized-12.pdf": pikepdf_objstm(12, linearize=True),
        "pymupdf-objstm-stored-9.pdf": pymupdf_objstm(9),
        "cupsfilter-text.pdf": cupsfilter_text(),
        "sips-image-1.pdf": sips_image(),
        "incremental-added-page-4.pdf": added_page(),
        "incremental-edited-page-3.pdf": edited_page(),
        "pdflib-indirect-length-1.pdf": pdflib_like(1),
        "pdflib-indirect-length-7.pdf": pdflib_like(7),
        "pymupdf-incremental-deleted-40-to-10.pdf": pymupdf_deleted(40, 10),
    }
    for name, data in made.items():
        (OUT / name).write_bytes(data)
        print(f"{name}: {len(data)} bytes, {EXPECTED.get(name) or OVERCOUNTED[name]} pages")


if __name__ == "__main__":
    main()
