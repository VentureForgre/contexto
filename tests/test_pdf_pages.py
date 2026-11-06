"""D60: a PDF is never estimated low. Its estimate is its pages times 4,568 when the pages are certain, else
600 pages (the API's maximum) with a ``pdf_pages_uncertain`` event.

On main a PDF was priced by its bytes as text, which undercounted compact files: at 1,583 tokens a page, 12
of the 14 producer fixtures (0.1 to 1.7 KB a page) were estimated at 0.02 to 0.41 of their cost pasted (a
7-page PDFlib file: 230 tokens for about 11,081). Each repro from PR #234's rounds 3 to 9 is here: every PDF
counts exactly and certain, or 600 and uncertain, and the lexer stays linear on hostile input (round 9: a
140-byte PDF parsed for 229 s through a backtracking token regex).
"""

from __future__ import annotations

import base64
import http.client
import json
import math
import random
import threading
import time
import tracemalloc
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from contexto import pdfpages, tokens
from contexto.canonical import canonical_json
from contexto.pdfpages import PDF_MAX_PAGES, PDF_MAX_STEPS, PdfCount, count_pages
from contexto.proxy import ContextoProxy
from contexto.rewrite import Config, RewriteEngine
from contexto.store import SessionStore
from contexto.store.memory import MemoryStore
from contexto.tokens import PDF_PAGE_TOKENS, estimate_message_tokens, pdf_page_count, split_media
from fixtures.make_pdfs import COUNTED_HIGH, EXPECTED, UNCERTAIN_FIXTURES
from test_image_estimate import HEAD, image_block, png_b64

EP = "/v1/messages"
FIXTURES = Path(__file__).parent / "fixtures" / "pdf"
TYPICAL = 1_583  # a page of the measured 3-page PDF


@pytest.fixture(autouse=True)
def _cold_cache():
    tokens._PDF_PAGES.clear()
    yield
    tokens._PDF_PAGES.clear()


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def doc(raw: bytes) -> dict:
    return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64(raw)}}


def count(raw: bytes) -> PdfCount:
    return pdf_page_count(b64(raw))


def objstm(number: int, objects: dict[int, bytes], *, flate: bool = True, extra: bytes = b"") -> bytes:
    """An object stream holding ``objects`` (number: value), with its offset table."""
    table, data = [], b""
    for n, value in objects.items():
        table.append(b"%d %d" % (n, len(data)))
        data += value + b"\n"
    header = b" ".join(table) + b"\n"
    packed = zlib.compress(header + data) if flate else header + data
    filters = b"/Filter /FlateDecode " if flate else b""
    return (b"%d 0 obj << /Type /ObjStm /N %d /First %d %s%s/Length %d >>\nstream\n" % (
        number, len(objects), len(header), filters, extra, len(packed)) + packed + b"\nendstream\nendobj\n")


def simple_pdf(pages: int, *, filler: int = 0, object_stream: bool = False) -> bytes:
    """A catalog, a flat page tree and ``pages`` page objects, with ``filler`` bytes of binary content."""
    page = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>"
    kids = b" ".join(b"%d 0 R" % (10 + i) for i in range(pages))
    out = b"%PDF-1.7\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
    out += b"2 0 obj << /Type /Pages /Count %d /Kids [ %s ] >> endobj\n" % (pages, kids)
    if object_stream:
        out += objstm(5, {10 + i: page for i in range(pages)})
    else:
        out += b"".join(b"%d 0 obj %s endobj\n" % (10 + i, page) for i in range(pages))
    if filler:
        data = random.Random(5).randbytes(filler)
        out += b"6 0 obj << /Length %d >>\nstream\n" % len(data) + data + b"\nendstream\nendobj\n"
    return out + b"trailer << /Root 1 0 R >>\n%%EOF\n"


def timed(raw: bytes) -> tuple[PdfCount, float]:
    start = time.perf_counter()
    result = count_pages(raw)
    return result, time.perf_counter() - start


def peak_memory(raw: bytes) -> int:
    """tracemalloc's peak while counting ``raw`` (the input itself is allocated before)."""
    tracemalloc.start()
    try:
        count_pages(raw)
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


# Real producers ------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_real_producers_count_exactly_and_certain(name):
    """reportlab (compressed and not), pikepdf/qpdf (object streams, linearized, QDF), PyMuPDF, fpdf2,
    macOS cupsfilter and sips, PDFlib-shaped and incremental updates."""
    assert count((FIXTURES / name).read_bytes()) == (min(EXPECTED[name], PDF_MAX_PAGES), True)


@pytest.mark.parametrize("name", sorted(COUNTED_HIGH))
def test_files_counted_high_are_never_counted_low(name):
    """An incremental save that deleted pages leaves the old page objects in the file: each number counts once,
    over every version, so these count high, certain."""
    result = count((FIXTURES / name).read_bytes())
    assert result.counted and result.pages >= COUNTED_HIGH[name]


@pytest.mark.parametrize("name", sorted(UNCERTAIN_FIXTURES))
def test_pages_that_show_pdf_objects_are_uncertain(name):
    """Rounds 6 and 7: uncompressed content streams that show a PDF's objects as text (``(3 0 obj) Tj``) read
    as the file's own (3ac7984: 1 page for 20, 5971e84: 2). An object header inside a stream body is one a
    reader could find by its offset, so the count is not certain."""
    assert count((FIXTURES / name).read_bytes()) == (PDF_MAX_PAGES, False)


# Round 1 to 3: bounds and object streams ----------------------------------------------------------

def test_a_count_with_thousands_of_digits_does_not_raise(tmp_path):
    raw = (b"%PDF-1.7\n1 0 obj <</Type/Pages/Count " + b"9" * 5000 + b" /Kids [2 0 R]>> endobj\n"
           b"2 0 obj <</Type/Page>> endobj")
    assert count(raw) == (PDF_MAX_PAGES, True)  # the count only raises; a huge one is the API's maximum
    eng = RewriteEngine(SessionStore(tmp_path, "pdf"), config=Config())
    req = {**HEAD, "messages": [{"role": "user", "content": [{"type": "text", "text": "read this"}, doc(raw)]}]}
    _, _, events = eng(req, {}, EP)  # v0.5.8 head: ValueError, and the proxy sent every request unwindowed
    assert any(e["event"] == "request" for e in events)


def test_line_wrapped_base64_is_counted():
    clean = b64(simple_pdf(100, filler=30_028))
    wrapped = "\n".join(clean[i:i + 76] for i in range(0, len(clean), 76))
    assert pdf_page_count(clean) == pdf_page_count(wrapped) == (100, True)


def test_object_streams_are_read_with_their_parameters():
    objects = {2: b"<< /Type /Pages /Count 100 /Kids [ %s ] >>" % b" ".join(b"%d 0 R" % (i + 3) for i in range(100))}
    objects.update({i + 3: b"<< /Type /Page /Parent 2 0 R >>" for i in range(100)})
    raw = b"%PDF-1.5\n" + objstm(900, objects, extra=b"/DecodeParms << /Columns 4 >> ")
    assert count(raw) == (100, True)  # 2ae9ff4: (1, False)
    stored = b"%PDF-1.5\n" + objstm(900, objects, flate=False)  # PyMuPDF writes object streams unfiltered
    assert count(stored) == (100, True)


@pytest.mark.parametrize("extra", [b"/DecodeParms << /Predictor 12 /Columns 4 >> ", b"/Filter2 /LZWDecode "])
def test_an_object_stream_that_cannot_be_read_is_uncertain(extra):
    objects = {2: b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>", 3: b"<< /Type /Page >>"}
    raw = b"%PDF-1.5\n" + objstm(900, {i + 4: b"<< /Type /Page >>" for i in range(50)}, extra=extra)
    if b"LZW" in extra:
        raw = raw.replace(b"/Filter /FlateDecode", b"/Filter /LZWDecode")
    raw += b"".join(b"%d 0 obj %s endobj\n" % (n, v) for n, v in objects.items())
    assert count(raw) == (PDF_MAX_PAGES, False)


def test_zlib_bombs_are_bounded():
    raw = b"%PDF-1.7\n" + b"".join(objstm(10 + i, {3: bytes(4 << 20)}) for i in range(50))  # 210 KB
    result, seconds = timed(raw)
    assert result == (PDF_MAX_PAGES, False) and seconds < 1
    assert peak_memory(raw) < 16 << 20  # one 4 MB stream at a time (2ae9ff4: 204 MB)


def test_the_inflate_budget_holds_once_it_is_spent_exactly(monkeypatch):
    """Round 9 (code read): with the budget spent exactly, ``room`` was 0, and zlib reads a max_length of 0 as
    no limit, so the next object stream inflated whole."""
    first = objstm(10, {3: b"<< /Type /Page >>"})
    size = len(zlib.decompress(first.split(b"stream\n")[1].split(b"\nendstream")[0]))
    monkeypatch.setattr(pdfpages, "PDF_INFLATE_BUDGET", size)
    raw = b"%PDF-1.7\n" + first + objstm(11, {4: bytes(64 << 20)}) + b"2 0 obj << /Type /Pages /Kids [3 0 R] >> endobj"
    result, seconds = timed(raw)
    assert result == (PDF_MAX_PAGES, False) and seconds < 1
    monkeypatch.setattr(pdfpages, "PDF_INFLATE_BUDGET", size)
    assert peak_memory(raw) < 4 << 20  # nothing was inflated past the budget
    monkeypatch.setattr(pdfpages, "PDF_INFLATE_BUDGET", 16 << 20)
    assert count_pages(b"%PDF-1.7\n" + first) == (1, True)


def _many_streams(pages: int) -> bytes:
    """Reviewer A's split8 shape: each page in its own Flate object stream, the root page tree written last."""
    page = b"<< /Type /Page /Parent 2 0 R >>"
    streams = b"".join(objstm(1_000 + i, {3 + i: page}) for i in range(pages))
    kids = b" ".join(b"%d 0 R" % (3 + i) for i in range(pages))
    return (b"%PDF-1.7\n" + streams + b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
            + b"2 0 obj << /Type /Pages /Count %d /Kids [ %s ] >> endobj\n" % (pages, kids))


def test_many_object_streams_are_read_within_the_inflate_budget(monkeypatch):
    """Round 8: a cap of 128 object streams let a leaf's /Count stand in for the document. Every object
    stream is read, within the inflate budget; past it the count is not certain."""
    raw = _many_streams(300)
    assert count_pages(raw) == (300, True)
    monkeypatch.setattr(pdfpages, "PDF_INFLATE_BUDGET", 5_000)
    assert count_pages(raw) == (PDF_MAX_PAGES, False)


def _split(pages: int, links: int) -> bytes:
    """Reviewer A's split8 files: object streams of 100 objects, link annotations beside each page, each
    6-kid leaf node written as it fills, the root last (true pages: ``pages``)."""
    objects: dict[int, bytes] = {1: b"<< /Type /Catalog /Pages 2 0 R >>"}
    number, leaves, kids = 10, [], []
    for p in range(pages):
        page_number, annots = number, list(range(number + 1, number + 1 + links))
        number += 1 + links
        refs = b" ".join(b"%d 0 R" % a for a in annots)
        objects[page_number] = b"<< /Type /Page /Parent %d 0 R /Annots [ %s ] >>" % (100_000 + len(leaves), refs)
        for a in annots:
            link = b"/A << /S /URI /URI (https://e.x/%d) >>" % a
            objects[a] = b"<< /Type /Annot /Subtype /Link /Rect [0 0 10 10] " + link + b" >>"
        kids.append(page_number)
        if len(kids) == 6 or p == pages - 1:
            objects[100_000 + len(leaves)] = b"<< /Type /Pages /Parent 2 0 R /Count %d /Kids [ %s ] >>" % (
                len(kids), b" ".join(b"%d 0 R" % k for k in kids))
            leaves.append(100_000 + len(leaves))
            kids = []
    objects[2] = b"<< /Type /Pages /Count %d /Kids [ %s ] >>" % (pages, b" ".join(b"%d 0 R" % n for n in leaves))
    order = [n for n in objects if n != 2] + [2]
    out = b"%PDF-1.7\n"
    for i in range(0, len(order), 100):
        out += objstm(500_000 + i, {n: objects[n] for n in order[i:i + 100]})
    return out + b"trailer << /Root 1 0 R >>\n%%EOF\n"


@pytest.mark.parametrize(("pages", "links"), [(200, 100), (300, 45), (450, 30), (600, 25), (600, 60)])
def test_split_object_streams_are_exact_or_uncertain(pages, links):
    """Round 8: 600 pages with 100 links a page counted 126, certain; reading it on a 900k bound was
    estimated at 687,851 for a real 1,062,103, with no event."""
    result = count_pages(_split(pages, links))
    assert result == (pages, True) or result == (PDF_MAX_PAGES, False)


def _object_stream_markers(bodies: list[bytes], *, stored: bool) -> bytes:
    header = b"%d 0 obj << /Type /ObjStm /N 1 /First 0 %s/Length %d >>\nstream\n"
    out = b"%PDF-1.7\n"
    for i, body in enumerate(bodies):
        data = body if stored else zlib.compress(body)
        out += header % (10 + i, b"" if stored else b"/Filter /FlateDecode ", len(data))
        out += data + b"\nendstream endobj\n"
    return out


TREE_MARKERS = b"/Type/Pages " * 999 + b"/Count " * 1_300
PAGE_MARKERS = b"/Type/Page " * 9_999


@pytest.mark.parametrize("bodies", [[TREE_MARKERS] * 128, [PAGE_MARKERS] * 128, [TREE_MARKERS, PAGE_MARKERS] * 64],
                         ids=["tree", "pages", "mixed"])
@pytest.mark.parametrize("stored", [False, True], ids=["flate", "stored"])
def test_marker_floods_in_object_streams_are_fast(bodies, stored):
    """Round 3: per-buffer caps multiplied across 128 object streams: 30 KB took 1.8 to 3.0 s."""
    result, seconds = timed(_object_stream_markers(bodies, stored=stored))
    assert result == (PDF_MAX_PAGES, False) and seconds < 1


# Rounds 4 to 7: page trees and stream lengths -----------------------------------------------------

def test_an_orphan_node_never_lowers_the_count():
    """Round 4: an orphan /Pages node with /Count 40 beside a 1-page file. Its count may raise, never lower."""
    raw = simple_pdf(1) + b"7 0 obj << /Type /Pages /Kids [] /Count 40 >> endobj\n"
    assert count_pages(raw) == (40, True)


def test_a_decoy_catalog_does_not_lower_the_count():
    """Round 6: an appended, unreferenced catalog and one-page tree took a 300-page file to 1 (3ac7984)."""
    raw = simple_pdf(300) + (b"900 0 obj << /Type /Catalog /Pages 901 0 R >> endobj\n"
                             b"901 0 obj << /Type /Pages /Count 1 /Kids [ 10 0 R ] >> endobj\n")
    assert count_pages(raw) == (300, True)


def test_a_crafted_wide_tree_is_bounded():
    """Round 6: keeping every node's /Kids list took a 990-node, 1,000-reference tree to 41 MB."""
    objs = [b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"]
    for n in range(990):
        kids = b" ".join(b"%d 0 R" % (2 + (n + 1 + k) % 990) for k in range(1_000))
        objs.append(b"%d 0 obj << /Type /Pages /Count 600 /Kids [ %s ] >> endobj\n" % (n + 2, kids))
    raw = b"%PDF-1.4\n" + b"".join(objs)
    result, seconds = timed(raw)
    assert result.pages == PDF_MAX_PAGES and seconds < 1 and peak_memory(raw) < 32 << 20


def _listing(pages: int, *, direct: bool) -> bytes:
    """Rounds 6 and 7: pages whose uncompressed content shows a minimal PDF's objects, after the real ones."""
    shown = b"BT (1 0 obj) Tj (<< /Type /Catalog /Pages 2 0 R >>) Tj (2 0 obj) Tj (<< /Type /Pages /Kids [3 0 R] " \
            b"/Count 1 >>) Tj (endstream) Tj ET"
    kids = b" ".join(b"%d 0 R" % (3 + 2 * i) for i in range(pages))
    objs = [b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
            b"2 0 obj << /Type /Pages /Count %d /Kids [ %s ] >> endobj\n" % (pages, kids)]
    for i in range(pages):
        page, content = 3 + 2 * i, 4 + 2 * i
        objs.append(b"%d 0 obj << /Type /Page /Parent 2 0 R /Contents %d 0 R >> endobj\n" % (page, content))
        length = b"/Length %d" % len(shown) if direct else b"/Length %d 0 R" % (5_000 + i)
        objs.append(b"%d 0 obj << %s >>\nstream\n" % (content, length) + shown + b"\nendstream\nendobj\n")
        if not direct:
            objs.append(b"%d 0 obj %d endobj\n" % (5_000 + i, len(shown)))
    return b"%PDF-1.4\n" + b"".join(objs) + b"trailer << /Root 1 0 R >>\n%%EOF\n"


@pytest.mark.parametrize("direct", [True, False], ids=["direct-length", "indirect-length"])
@pytest.mark.parametrize("pages", [20, 100])
def test_objects_shown_inside_content_streams_are_uncertain(pages, direct):
    assert count_pages(_listing(pages, direct=direct)) == (PDF_MAX_PAGES, False)


def test_an_indirect_length_is_resolved_and_checked():
    """qpdf --qdf and PDFlib write each /Length as an object after its stream: the stream is read at that
    length, and the object must finally hold it."""
    body = b"BT (hello) Tj ET"
    raw = (b"%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
           b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
           b"3 0 obj << /Type /Page /Contents 4 0 R >> endobj\n"
           b"4 0 obj << /Length 5 0 R >>\nstream\n" + body + b"\nendstream\nendobj\n5 0 obj %d endobj\n" % len(body))
    assert count_pages(raw) == (1, True)
    redefined = raw + b"5 0 obj %d endobj\n" % (len(body) + 3)  # a later version: the length it was read at is gone
    assert count_pages(redefined) == (PDF_MAX_PAGES, False)
    assert count_pages(raw.replace(b"5 0 obj", b"6 0 obj")) == (PDF_MAX_PAGES, False)  # never defined


def _hostile_stream(pages: int, mode: str) -> bytes:
    """Round 7: a correct xref, the first content stream before the page objects, with a /Length that lies
    long to the last endstream (covering them) or no endstream at all."""
    content = b"BT /F1 10 Tf 72 700 Td (hi) Tj ET"
    first = 3 + pages
    pages_part = b"".join(b"%d 0 obj << /Type /Page /Parent 2 0 R /Contents %d 0 R >> endobj\n" % (3 + i, first)
                          for i in range(pages))
    tree = b"2 0 obj << /Type /Pages /Kids [ %s ] /Count %d >> endobj\n" % (
        b" ".join(b"%d 0 R" % (3 + i) for i in range(pages)), pages)
    head = b"%PDF-1.7\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
    if mode == "long-length":
        covered = content + b"\nendstream\nendobj\n" + tree + pages_part + b"%d 0 obj << /Length 0 >>\nstream\n" % (
            first + 1)
        stream = b"%d 0 obj << /Length %d >>\nstream\n" % (first, len(covered))
        return head + stream + covered + b"\nendstream\nendobj\n"
    return (head + b"%d 0 obj << /Length 99 0 R >>\nstream\n" % first + content + b"\nendobj\n" + tree + pages_part
            + b"99 0 obj %d endobj\n" % len(content))


@pytest.mark.parametrize("mode", ["long-length", "missing-endstream"])
@pytest.mark.parametrize("pages", [20, 300])
def test_lying_stream_lengths_are_uncertain(pages, mode):
    """Round 7 (reviewer A): these counted 1 to 12 pages for 20 or 300, certain."""
    assert count_pages(_hostile_stream(pages, mode)) == (PDF_MAX_PAGES, False)


HIDING = {"plain": b"5 0 obj", "comments": b"5 %c\n0 %d\nobj", "nul": b"5\x000\x00obj"}


# On one comment line a reader would take the first % as the end of the object, so "comments" is not hidden there
@pytest.mark.parametrize(("where", "hiding"), [(w, h) for w in ("body", "string", "comment") for h in HIDING
                                               if (w, h) != ("comment", "comments")])
def test_an_object_header_where_the_lexer_does_not_look_is_uncertain(where, hiding):
    """A reader that follows the xref finds an object at its offset wherever it sits: inside a stream body
    whose /Length lies long, a string, or a comment. A header there, however it is spaced, is uncertain."""
    hiding = HIDING[hiding]
    hidden = hiding + b" << /Type /Page /Parent 2 0 R >> endobj "
    base = simple_pdf(1)
    if where == "body":
        extra = b"8 0 obj << /Length %d >>\nstream\n" % (len(hidden) + 4)
        extra += b"xx\n" + hidden + b"\n\nendstream\nendobj\n"
    elif where == "string":
        extra = b"8 0 obj << /Title (" + hidden + b") >> endobj\n"
    else:
        extra = b"%" + hidden.replace(b"\n", b" ") + b"\n"
    assert count_pages(base) == (1, True)
    assert count_pages(base + extra) == (PDF_MAX_PAGES, False)


def test_words_that_are_not_headers_stay_certain():
    raw = simple_pdf(2) + b"8 0 obj << /Title (an object, objects, the obj file) /Subject /obj >> endobj\n% objective\n"
    assert count_pages(raw) == (2, True)


# Round 8: syntax that hid pages from the regexes ----------------------------------------------------

HIDDEN_PAGES = 50


def _hidden_doc(page_type: bytes = b"/Type /Page", tree_type: bytes = b"/Type /Pages", count_key: bytes = b"/Count",
                *, extra: dict | None = None, tree_extra: bytes = b"", page_dict=None) -> dict[int, bytes]:
    """Reviewer A's 50-page document: 1 catalog, 2 page tree, 3 font, 10.. pages, 1000.. contents."""
    kids = b" ".join(b"%d 0 R" % (10 + i) for i in range(HIDDEN_PAGES))
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>", 3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            2: b"<< " + tree_type + b" " + tree_extra + b" /Kids [" + kids + b"] " + count_key
               + b" %d >>" % HIDDEN_PAGES}
    for i in range(HIDDEN_PAGES):
        text = b"BT /F1 12 Tf 72 720 Td (Page %d) Tj ET" % (i + 1)
        objs[1000 + i] = b"<< /Length %d >>\nstream\n" % len(text) + text + b"\nendstream"
        kind = page_dict(i) if page_dict else page_type
        objs[10 + i] = (b"<< " + kind + b" /Parent 2 0 R /MediaBox [0 0 612 792]"
                        + b" /Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % (1000 + i))
    objs.update(extra or {})
    return objs


def _hidden_pdf(objs: dict[int, bytes], *, stream: dict | None = None) -> bytes:
    """``objs`` written in order; with ``stream`` (members, filter, encode, pad), the members go in one object
    stream instead."""
    members = set(stream["members"]) if stream else set()
    out = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"
    for number in sorted(objs):
        if number not in members:
            out += b"%d 0 obj\n" % number + objs[number] + b"\nendobj\n"
    if stream:
        table, body = [], b""
        for number in stream["members"]:
            table.append(b"%d %d" % (number, len(body)))
            body += objs[number] + b"\n"
        head = b" ".join(table) + b"\n"
        encoded = stream["encode"](head + body + b" " * stream.get("pad", 0))
        out += (b"4000 0 obj\n<< /Type /ObjStm /N %d /First %d " % (len(stream["members"]), len(head))
                + stream["filter"] + b" /Length %d >>\nstream\n" % len(encoded) + encoded + b"\nendstream\nendobj\n")
    return out + b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"


def _in_stream(filters: bytes, encode, *, pad: int = 0) -> bytes:
    objs = _hidden_doc(extra={5000: b"<< /Type /Pages /Kids [] /Count 1 >>"})
    members = [2] + [10 + i for i in range(HIDDEN_PAGES)]
    return _hidden_pdf(objs, stream={"members": members, "filter": filters, "encode": encode, "pad": pad})


def _hex(raw: bytes) -> bytes:
    return raw.hex().encode() + b">"


HIDDEN = {
    "comment": lambda: _hidden_pdf(_hidden_doc(b"/Type %c\n/Page", b"/Type %c\n/Pages", b"/Count %c\n")),
    "escaped-keys": lambda: _hidden_pdf(_hidden_doc(b"/T#79pe /Page", b"/T#79pe /Pages", b"/C#6funt")),
    "escaped-values": lambda: _hidden_pdf(_hidden_doc(b"/Type /P#61ge", b"/Type /P#61ges")),
    "17-spaces": lambda: _hidden_pdf(_hidden_doc(b"/Type" + b" " * 17 + b"/Page", b"/Type" + b" " * 17 + b"/Pages")),
    "nul-whitespace": lambda: _hidden_pdf(_hidden_doc(b"/Type\x00/Page", b"/Type\x00/Pages")),
    "indirect-type": lambda: _hidden_pdf(_hidden_doc(b"/Type 6 0 R", b"/Type 7 0 R",
                                                     extra={6: b"/Page", 7: b"/Pages"})),
    "untyped-pages": lambda: _hidden_pdf(_hidden_doc(b"", b"/Type /Pages", b"/Count %c\n")),
    "no-type-at-all": lambda: _hidden_pdf(_hidden_doc(b"", b"", b"/Count %c\n")),
    "untyped-tree-hidden-pages": lambda: _hidden_pdf(_hidden_doc(
        page_dict=lambda i: b"/Type /Page" if i < 3 else b"", tree_type=b"")),
    "nested-before-count": lambda: _hidden_pdf(_hidden_doc(b"/Type %c\n/Page",
                                                           tree_extra=b"/Resources << /F 3 0 R >>")),
    "3-visible-47-hidden": lambda: _hidden_pdf(_hidden_doc(
        page_dict=lambda i: b"/Type /Page" if i < 3 else b"/Type %c\n/Page", count_key=b"/Count %c\n")),
    "all-hidden-orphan": lambda: _hidden_pdf(_hidden_doc(
        b"/Type %c\n/Page", extra={5000: b"<< /Type /Pages /Kids [] /Count 1 >>"}, count_key=b"/Count %c\n")),
    "objstm-hex-flate": lambda: _in_stream(b"/Filter [/ASCIIHexDecode /FlateDecode]", lambda r: _hex(zlib.compress(r))),
    "objstm-hex": lambda: _in_stream(b"/Filter /ASCIIHexDecode", _hex),
    "objstm-over-4mb": lambda: _in_stream(b"/Filter /FlateDecode", zlib.compress, pad=5 << 20),
    "objstm-escaped-filter": lambda: _in_stream(b"/Filter /Fl#61teDecode", zlib.compress),
    "objstm-untyped": lambda: _in_stream(b"/Filter /FlateDecode", zlib.compress).replace(b"/Type /ObjStm ", b""),
}


@pytest.mark.parametrize("case", sorted(HIDDEN))
def test_pdf_syntax_never_hides_pages(case):
    """Round 8 (reviewer A's hide8): pages written in valid ways a regex missed counted 1 to 12 of 50, and an
    object stream not read let an orphan /Count 1 stand in for it. Each is exact and certain, or 600."""
    result = count_pages(HIDDEN[case]())
    assert result == (HIDDEN_PAGES, True) or result == (PDF_MAX_PAGES, False)


def test_most_syntax_variants_are_read_exactly():
    """The lexer reads them, not only fails safe: only the filters it does not decode are uncertain."""
    exact = {case for case in HIDDEN if count_pages(HIDDEN[case]()) == (HIDDEN_PAGES, True)}
    assert set(HIDDEN) - exact == {"objstm-hex-flate", "objstm-hex", "objstm-over-4mb"}
    assert count_pages(simple_pdf(3).replace(b"/Count 3", b"/Count +3")) == (3, True)  # a signed integer
    assert count_pages(simple_pdf(650)) == (PDF_MAX_PAGES, True)  # over the API's limit


def test_form_fields_and_name_trees_are_not_pages():
    """A /Kids the page tree does not reach (a form field's, a name tree's) lists no pages."""
    raw = simple_pdf(2) + (b"20 0 obj << /Fields [21 0 R] >> endobj\n21 0 obj << /T (f) /Kids [22 0 R 23 0 R 24 0 R] >>"
                           b" endobj\n30 0 obj << /Kids [31 0 R 32 0 R] >> endobj\n31 0 obj << /Names [] >> endobj\n")
    assert count_pages(raw) == (2, True)


# PR #248 round 1 (reviewer A): pages listed twice, and a dead copy of an indirect value ------------------

def _xref_pdf(objects: list[tuple[int, bytes]], *, dead: list[tuple[int, bytes]] = (), root: int = 1) -> bytes:
    """``objects`` (number, value or object stream) with a correct xref table, then ``dead`` objects that no
    xref entry names, so a reader that follows the xref (qpdf, pypdf) never takes them."""
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for number, value in objects:
        offsets[number] = len(out)
        out += value if value.startswith(b"%d 0 obj" % number) else b"%d 0 obj\n%s\nendobj\n" % (number, value)
    for number, value in dead:
        out += value if value.startswith(b"%d 0 obj" % number) else b"%d 0 obj\n%s\nendobj\n" % (number, value)
    xref, size = len(out), max(offsets) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    out += b"".join(b"%010d 00000 n \n" % offsets[n] if n in offsets else b"0000000000 65535 f \n"
                    for n in range(1, size))
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, root, xref)
    return bytes(out)


PAGE = b"<< /Parent 2 0 R /MediaBox [0 0 612 792] >>"  # untyped: qpdf, pypdf and MuPDF read it as a page


def _repeated_page(times: int) -> bytes:
    """One page object listed ``times`` in the root's /Kids, /Count 1: qpdf and pypdf read ``times`` pages."""
    return _xref_pdf([(1, b"<< /Type /Catalog /Pages 2 0 R >>"),
                      (2, b"<< /Type /Pages /Kids [ %s ] /Count 1 >>" % b" ".join([b"4 0 R"] * times)),
                      (4, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>")])


def _dead_copy(placement: str) -> bytes:
    """The root's /Kids is ``900 0 R``, an array of 50 untyped pages; a later copy of 900 that no xref names
    lists one. With ``type`` the pages' /Type is ``902 0 R`` (/Page) and the dead 902 is /Pagex."""
    objects = [(1, b"<< /Type /Catalog /Pages 2 0 R >>"), (2, b"<< /Type /Pages /Kids 900 0 R /Count 1 >>"),
               (900, b"[ %s ]" % b" ".join(b"%d 0 R" % (10 + i) for i in range(50)))]
    page = PAGE if placement != "type" else PAGE.replace(b"<< ", b"<< /Type 902 0 R ")
    objects += [(10 + i, page) for i in range(50)]
    dead = [(900, b"[ 10 0 R ]")]
    if placement == "type":
        objects.append((902, b"/Page"))
        dead = [(902, b"/Pagex"), (900, b"[ 10 0 R ]")]
    elif placement == "object-stream":
        dead = [(5_000, objstm(5_000, {900: b"[ 10 0 R ]"}))]
    elif placement == "untyped-root":
        objects[1] = (2, b"<< /Kids 900 0 R /Count 1 >>")
    return _xref_pdf(objects, dead=dead)


def test_a_page_listed_many_times_counts_each_time():
    """A renderer counts every /Kids entry that reaches a page: 50 entries of one page object are 50 pages
    (a647487: 1, certain; read at 90,865 on 150k it was estimated at 95,472 for a real 170,074, unflagged)."""
    assert count_pages(_repeated_page(50)) == (50, True)
    assert count_pages(_repeated_page(700)) == (PDF_MAX_PAGES, True)
    # Shared below two nodes, a page counts under each
    raw = _xref_pdf([(1, b"<< /Type /Catalog /Pages 2 0 R >>"), (2, b"<< /Type /Pages /Kids [3 0 R 5 0 R] >>"),
                     (3, b"<< /Type /Pages /Parent 2 0 R /Kids [4 0 R 4 0 R] >>"),
                     (5, b"<< /Type /Pages /Parent 2 0 R /Kids [4 0 R 3 0 R] >>"), (4, PAGE)])
    assert count_pages(raw) == (5, True)


@pytest.mark.parametrize("child_kids", [b"[4 0 R 3 0 R]", b"[4 0 R 2 0 R]"], ids=["self", "to-the-root"])
def test_a_cycle_in_the_page_tree_is_uncertain(child_kids):
    raw = _xref_pdf([(1, b"<< /Type /Catalog /Pages 2 0 R >>"), (2, b"<< /Type /Pages /Kids [3 0 R 4 0 R] >>"),
                     (3, b"<< /Type /Pages /Parent 2 0 R /Kids %s >>" % child_kids), (4, PAGE)])
    assert count_pages(raw) == (PDF_MAX_PAGES, False)


@pytest.mark.parametrize("placement", ["later", "object-stream", "type", "untyped-root"])
def test_a_dead_copy_of_an_indirect_value_never_hides_pages(placement):
    """An indirect /Kids or /Type resolved to the copy read last, not the one the xref names, so a later
    dead copy hid 49 of 50 pages (a647487: 1, certain). Every version of the target counts."""
    assert count_pages(_dead_copy(placement)) == (50, True)


def test_versions_of_a_structural_value_must_agree():
    """An object stream's /N, or a stream's indirect /Length, with two versions that differ is uncertain."""
    stream = objstm(5, {10: b"<< /Type /Page >>"}).replace(b"/N 1", b"/N 7 0 R")
    raw = b"%PDF-1.7\n7 0 obj 1 endobj\n" + stream
    assert count_pages(raw) == (1, True)
    assert count_pages(raw + b"7 0 obj 2 endobj\n") == (PDF_MAX_PAGES, False)
    body = b"BT ET"
    page = b"1 0 obj << /Type /Page /Contents 4 0 R >> endobj\n4 0 obj << /Length 5 0 R >>\nstream\n" + body
    page += b"\nendstream\nendobj\n5 0 obj %d endobj\n" % len(body)
    assert count_pages(b"%PDF-1.7\n" + page) == (1, True)
    assert count_pages(b"%PDF-1.7\n" + page + b"5 0 obj 99 endobj\n") == (PDF_MAX_PAGES, False)


@pytest.mark.parametrize("raw", [_repeated_page(50), _dead_copy("later")], ids=["repeated", "dead-copy"])
@pytest.mark.parametrize("pasted", [True, False], ids=["pasted", "read"])
def test_reviewer_a_files_near_the_bound_are_never_estimated_low(tmp_path, raw, pasted):
    seen = _session(tmp_path, raw, 50 * TYPICAL, pasted=pasted)
    carrying = [(estimate, real) for estimate, real, _, carries in seen if carries]
    assert carrying[0][0] - carrying[0][1] >= 50 * (PDF_PAGE_TOKENS - TYPICAL) - 50
    assert all(estimate >= real * 0.995 for estimate, real in carrying)


# Round 9: the lexer is linear, its memory flat, and its work bounded ------------------------------------------

ROUND_9 = {
    "64 % then )": b"%PDF-1.7\n" + b"%" * 64 + b"\n)",
    "% space x 64": b"%PDF-1.7\n" + b"% " * 64 + b")",
    "30 % in an object": b"%PDF-1.7\n1 0 obj " + b"%" * 30 + b"\n)",
    "1 MB of digits then x": b"%PDF-1.7\n" + b"1" * (1 << 20) + b"x",
    "[ 10,000 digits x]": b"%PDF-1.7\n1 0 obj [" + b"1" * 10_000 + b"x] endobj",
}


@pytest.mark.parametrize("case", sorted(ROUND_9))
def test_backtracking_inputs_are_instant(case):
    """Round 9: 20 x ``% `` took over 60 s, and 40,000 digits 7.6 s (a token regex with nested quantifiers)."""
    result, seconds = timed(ROUND_9[case])
    assert result == (PDF_MAX_PAGES, False) and seconds < 0.1


PADDING = {"spaces": b" " * (16 << 20), "NULs": b"\x00" * (16 << 20), "CRLFs": b"\r\n" * (8 << 20),
           "comment lines": b"%\n" * (8 << 20)}


@pytest.mark.parametrize("padding", sorted(PADDING))
def test_padding_costs_no_memory(padding):
    """Round 9: 16 MB of ``%\\n`` peaked at 2,560 MB (sre kept a frame per repeat). Skipped runs make no
    objects. A long run of whitespace is one step, so the file stays certain; a run of comments spends the
    step budget."""
    raw = simple_pdf(1).replace(b"2 0 obj", b"2 0 obj" + PADDING[padding])
    result, seconds = timed(raw)
    assert seconds < 1 and peak_memory(raw) < 4 << 20
    assert result == ((PDF_MAX_PAGES, False) if padding == "comment lines" else (1, True))


HOSTILE = {
    "tokens": b"%PDF-1.7\n" + b"/Count 1 " * 2_000_000,
    "kept array": b"%PDF-1.7\n1 0 obj [" + b"1 " * (8 << 20) + b"] endobj",
    "names in a skipped array": b"%PDF-1.7\n1 0 obj << /X [" + b"/a" * (8 << 20) + b"] >> endobj",
    "objects": b"%PDF-1.7\n" + b"1 0 obj 1 endobj\n" * 1_000_000,
    "string of escapes": b"%PDF-1.7\n1 0 obj (" + b"\\(" * (4 << 20) + b") endobj",
    "nested strings": b"%PDF-1.7\n1 0 obj (" + b"()" * (4 << 20) + b") endobj",
    "huge string": b"%PDF-1.7\n1 0 obj (" + b"a" * (16 << 20) + b") endobj",
    "deep nesting": b"%PDF-1.7\n1 0 obj " + b"[" * 1_000_000,
    "deep skipped nesting": b"%PDF-1.7\n1 0 obj << /Foo " + b"<< /A " * 1_000_000,
    "obj in a body": (b"%PDF-1.7\n" + b"1 0 obj << /Length %d >>\nstream\n" % (12 << 20) + b"obj" * (4 << 20)
                      + b"\nendstream"),
    "endobj flood for an indirect length": b"%PDF-1.7\n5 0 obj << /Length 7 0 R >>\nstream\nxx\nendstream endobj "
                                           + b"endobj " * 2_000_000,
}


@pytest.mark.parametrize("case", sorted(HOSTILE))
def test_hostile_pdfs_stop_at_the_step_budget(case):
    """Each makes the lexer take a step for every few bytes: past ``PDF_MAX_STEPS`` the count is uncertain,
    well under a second (about 0.2 s here at most), with flat memory."""
    raw = HOSTILE[case]
    result, seconds = timed(raw)
    assert result == (PDF_MAX_PAGES, False) and seconds < 1
    reader = pdfpages._Reader(raw)
    with pytest.raises(pdfpages._Uncertain):
        reader.read_file()
        reader.counted()
    assert reader.steps >= -1


def test_twenty_hostile_pdfs_at_once_keep_memory_flat():
    """Round 9: 20 threads on distinct 1 MB comment PDFs peaked at 3,175 MB together."""
    raws = [b"%PDF-1.7\n%" + bytes([65 + i]) + b"\n" + b"%\n" * (64 << 10) + b" " * (1 << 20) for i in range(20)]
    tracemalloc.start()
    try:
        start = time.perf_counter()
        threads = [threading.Thread(target=count_pages, args=(raw,)) for raw in raws]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        seconds = time.perf_counter() - start
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 16 << 20 and seconds < 30


def test_real_pdfs_fit_the_step_budget():
    """The largest real files take a fraction of the budget (a 600-page PDF with 25 links a page about
    340,000, measured in D60), so a real PDF is not uncertain for its size."""
    for name in EXPECTED:
        reader = pdfpages._Reader((FIXTURES / name).read_bytes())
        reader.read_file()
        reader.counted()
        assert PDF_MAX_STEPS - reader.steps < PDF_MAX_STEPS // 20


# The estimate and the session -----------------------------------------------------------------------

def _session(tmp_path, raw: bytes, cost: int, *, bound: int = 150_000, pasted: bool = True) -> list[tuple]:
    """Text at 3.0 bytes a token, the PDF at ``cost`` tokens; the PDF pasted (or read) at the fourth request,
    then Reads until a request crosses the bound. Returns (estimate, real, over_bound flagged, carries the
    PDF) for every request."""
    eng = RewriteEngine(SessionStore(tmp_path, "pdf"), config=Config(bound=bound))
    rng = random.Random(9)
    block = doc(raw)
    msgs: list[dict] = [{"role": "user", "content": [{"type": "text", "text": "read the report"}]}]
    seen = []
    for step in range(200):
        req = {**HEAD, "messages": list(msgs)}
        upstream, _, events = eng(req, {}, EP)
        stripped, found = split_media(upstream["messages"])
        real = math.ceil(len(canonical_json({**upstream, "messages": stripped})) / 3.0) + cost * len(found)
        estimate = next(e for e in events if e["event"] == "request")["estimate"]
        seen.append((estimate, real, any(e["event"] == "over_bound" for e in events), bool(found)))
        if real > bound:
            return seen
        eng.observe_usage(EP, {"input_tokens": real, "output_tokens": 10}, req)
        use = {"type": "tool_use", "id": f"t{step}", "name": "Read", "input": {}}
        if step == 3 and pasted:
            msgs += [{"role": "assistant", "content": [{"type": "text", "text": "send it"}]},
                     {"role": "user", "content": [{"type": "text", "text": "here"}, block]}]
            continue
        content = [block] if step == 3 else " ".join(rng.choices(["def", "x", "=", "return"], k=5_000))
        msgs += [{"role": "assistant", "content": [use]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{step}", "content": content}]}]
    raise AssertionError("never crossed the bound")


@pytest.mark.parametrize("name", ["pdflib-indirect-length-7.pdf", "fpdf2-5.pdf", "pikepdf-objstm-12.pdf"])
@pytest.mark.parametrize("cost", [TYPICAL, PDF_PAGE_TOKENS])
@pytest.mark.parametrize("pasted", [True, False], ids=["pasted", "read"])
def test_a_compact_pdf_is_never_estimated_low(tmp_path, name, cost, pasted):
    """On main the 7-page PDFlib file (535 bytes) was estimated at 230 tokens for about 11,081 (1,583 a page)
    pasted. Every request carrying a compact PDF is now estimated at or over its real usage, and the request
    that crosses the bound is flagged before the send."""
    pages = EXPECTED[name]
    seen = _session(tmp_path, (FIXTURES / name).read_bytes(), pages * cost, pasted=pasted)
    carrying = [(estimate, real) for estimate, real, _, carries in seen if carries]
    # The request that brings it in: the PDF at its allowance, the text beside it within rounding of the ratio
    assert carrying[0][0] - carrying[0][1] >= pages * (PDF_PAGE_TOKENS - cost) - 50
    # Later requests build on the measured usage, which holds the PDF's real cost: within text rounding
    assert all(estimate >= real * 0.995 for estimate, real in carrying)


@pytest.mark.parametrize("pasted", [True, False], ids=["pasted", "read"])
def test_a_compact_pdf_near_the_bound_is_flagged_before_the_send(tmp_path, pasted):
    """The 7-page PDFlib file (real 11,081 at 1,583 a page) arrives when the conversation is under 9,000
    tokens from the bound, so the request crosses it. On main its bytes priced it at 230: read in a tool
    loop the request went out over the bound unflagged, and pasted at a turn start nothing was paged for it.
    Now a Read is flagged before the send (rule 5's tool-loop exception), and a paste pages enough at the turn
    start for the request to fit."""
    raw = (FIXTURES / "pdflib-indirect-length-7.pdf").read_bytes()
    eng = RewriteEngine(SessionStore(tmp_path, "pdf"), config=Config(bound=150_000))
    rng = random.Random(3)
    msgs: list[dict] = [{"role": "user", "content": [{"type": "text", "text": "start"}]}]
    real = 0
    for step in range(100):
        req = {**HEAD, "messages": list(msgs)}
        upstream, _, events = eng(req, {}, EP)
        real = math.ceil(len(canonical_json(upstream)) / 3.0)
        eng.observe_usage(EP, {"input_tokens": real, "output_tokens": 10}, req)
        if real > 150_000 - 9_000:
            break
        use = {"type": "tool_use", "id": f"t{step}", "name": "Read", "input": {}}
        msgs += [{"role": "assistant", "content": [use]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{step}",
                                               "content": " ".join(rng.choices(["a", "bb", "c"], k=3_000))}]}]
    assert 150_000 - 9_000 < real <= 150_000
    if pasted:
        msgs += [{"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
                 {"role": "user", "content": [{"type": "text", "text": "and this"}, doc(raw)]}]
    else:
        msgs += [{"role": "assistant", "content": [{"type": "tool_use", "id": "pdf", "name": "Read", "input": {}}]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "pdf", "content": [doc(raw)]}]}]
    upstream, _, events = eng({**HEAD, "messages": list(msgs)}, {}, EP)
    stripped, found = split_media(upstream["messages"])
    sent = math.ceil(len(canonical_json({**upstream, "messages": stripped})) / 3.0) + 7 * TYPICAL * len(found)
    request = next(e for e in events if e["event"] == "request")
    assert found and request.get("sent_estimate", request["estimate"]) >= sent
    if pasted:
        assert sent <= 150_000 and any(e["event"] == "rotation" for e in events)
    else:
        assert sent > 150_000 and request.get("over_bound")
        assert any(e["event"] == "over_bound" and e["source"] == "estimate" and e["in_tool_loop"] for e in events)
    assert not any(e["event"] == "pdf_pages_uncertain" for e in events)


def test_an_uncertain_pdf_is_logged_and_priced_at_the_maximum(tmp_path):
    eng = RewriteEngine(SessionStore(tmp_path, "pdf"), config=Config())
    req = {**HEAD, "messages": [{"role": "user", "content": [{"type": "text", "text": "read this"},
                                                              doc(HIDDEN["objstm-hex"]())]}]}
    _, _, events = eng(req, {}, EP)
    (uncertain,) = [e for e in events if e["event"] == "pdf_pages_uncertain"]
    assert uncertain["documents"] == 1 and uncertain["pages"] == PDF_MAX_PAGES
    assert next(e for e in events if e["event"] == "request")["estimate"] >= PDF_MAX_PAGES * PDF_PAGE_TOKENS
    # Logged when the PDF arrives, not again on every later request that carries it
    req["messages"] += [{"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
                        {"role": "user", "content": [{"type": "text", "text": "and?"}]}]
    _, _, events = eng(req, {}, EP)
    assert not [e for e in events if e["event"] == "pdf_pages_uncertain"]
    certain = {**HEAD, "messages": [{"role": "user", "content": [{"type": "text", "text": "read this"},
                                                                  doc(_hidden_pdf(_hidden_doc()))]}]}
    _, _, events = RewriteEngine(SessionStore(tmp_path / "certain", "pdf"), config=Config())(certain, {}, EP)
    assert not [e for e in events if e["event"] == "pdf_pages_uncertain"]


def test_documents_by_reference_are_uncertain(tmp_path):
    url = {"type": "document", "source": {"type": "url", "url": "https://example.com/a.pdf"}}
    file = {"type": "document", "source": {"type": "file", "file_id": "file_011"}}
    eng = RewriteEngine(SessionStore(tmp_path, "pdf"), config=Config())
    _, _, events = eng({**HEAD, "messages": [{"role": "user", "content": [url, file]}]}, {}, EP)
    (uncertain,) = [e for e in events if e["event"] == "pdf_pages_uncertain"]
    assert uncertain["documents"] == 2


def test_hidden_pages_read_near_the_bound_are_flagged(tmp_path):
    """Round 8 (reviewer A, e2e8 hidden50): estimated 137,531 for a real 166,453 on 150k, with no event."""
    seen = _session(tmp_path, HIDDEN["3-visible-47-hidden"](), HIDDEN_PAGES * TYPICAL, pasted=False)
    carrying = [(estimate, real, flagged) for estimate, real, flagged, carries in seen if carries]
    assert carrying[0][0] - carrying[0][1] >= HIDDEN_PAGES * (PDF_PAGE_TOKENS - TYPICAL) - 50
    assert all(estimate >= real * 0.995 for estimate, real, _ in carrying)
    assert carrying[-1][2]  # the request over the bound


def test_the_qdf_listing_read_near_the_bound_is_flagged(tmp_path):
    """Round 7 (reviewer A): reading a 100-page qpdf --qdf listing was estimated at 44,755 for a real 193,939
    on 150k, unflagged."""
    seen = _session(tmp_path, (FIXTURES / "qdf-listing-20.pdf").read_bytes(), 20 * TYPICAL, pasted=False)
    carrying = [(estimate, real, flagged) for estimate, real, flagged, carries in seen if carries]
    assert carrying[0][0] >= PDF_MAX_PAGES * PDF_PAGE_TOKENS and carrying[0][2]  # uncertain: 600 pages, flagged
    assert all(estimate >= real * 0.995 for estimate, real, _ in carrying)


def test_media_free_and_image_only_requests_never_count_pages(tmp_path, monkeypatch):
    """A session without PDFs runs no PDF code: its estimates and bodies are main's."""
    calls = []
    monkeypatch.setattr(tokens, "count_pages", lambda raw: calls.append(1))
    eng = RewriteEngine(SessionStore(tmp_path, "pdf"), config=Config())
    eng.prime({"messages": [{"role": "user", "content": [image_block(data=png_b64(payload=100))]}]})
    eng({**HEAD, "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"},
                                                            image_block(data=png_b64(payload=100))]}]}, {}, EP)
    assert not calls


# Priming before the proxy's lock, and the cache ---------------------------------------------------------

class _Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        data = b'{"type":"message","content":[],"usage":{"input_tokens":5,"output_tokens":1}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _body(messages: list[dict]) -> bytes:
    return json.dumps({**HEAD, "stream": False, "messages": messages}).encode()


def _post(port: int, body: bytes) -> float:
    start = time.perf_counter()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        conn.request("POST", EP, body=body, headers={"Content-Type": "application/json", "x-api-key": "synthetic"})
        assert conn.getresponse().status == 200
    finally:
        conn.close()
    return time.perf_counter() - start


def test_the_proxy_counts_pdfs_before_it_takes_the_transform_lock(monkeypatch):
    """#234 round 3: ``prime`` cached nothing, so every PDF was parsed under the lock every session waits on.
    Every parse runs with the lock free, and the cache holds the request's PDFs when the transform starts."""
    parses: list[bool] = []
    real = tokens.count_pages
    monkeypatch.setattr(tokens, "count_pages", lambda raw: parses.append(proxy.transform_lock.locked()) or real(raw))
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=Config())
    seen: list[tuple] = []

    class Spy:
        preserves_input = True

        def prime(self, request):
            engine.prime(request)
            seen.append(("primed", len(parses)))

        def __call__(self, request, headers, endpoint):
            seen.append(("transform", proxy.transform_lock.locked(), len(parses)))
            return engine(request, headers, endpoint)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    proxy = ContextoProxy(f"http://127.0.0.1:{server.server_address[1]}", MemoryStore(key=b"k" * 32), Spy())
    proxy.start()
    try:
        pasted = {"role": "user", "content": [doc(simple_pdf(3, filler=100))]}
        use = {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}}]}
        read = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                             "content": [doc(simple_pdf(4, filler=100))]}]}
        _post(proxy.port, _body([pasted, use, read]))
    finally:
        proxy.stop()
        server.shutdown()
        server.server_close()
    assert seen == [("primed", 2), ("transform", True, 2)]  # both PDFs counted before the lock, none after
    assert parses == [False, False]


def test_hostile_pdfs_do_not_hold_up_another_session():
    """Round 9: 20 PDFs of 24 x ``%`` held another session's text request for 48 s (one of 30 x ``%``, 114 s).
    Twenty sessions each post a hostile PDF of 200,000 tokens; they are parsed outside the transform lock, one
    at a time, pausing for the GIL, so a text request from another session waits on none of them (0.005 to
    0.12 s measured with the clients in another process; main 0.014 s)."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    proxy = ContextoProxy(f"http://127.0.0.1:{server.server_address[1]}", MemoryStore(key=b"k" * 32),
                          RewriteEngine(MemoryStore(key=b"k" * 32), config=Config()))
    proxy.start()
    try:
        bodies = [_body([{"role": "user", "content": [doc(b"%PDF-1.7\n%" + b"%" * 24 + bytes([65 + i]) + b"\n"
                                                         + b"/Count 1 " * 100_000)]}]) for i in range(20)]
        text = _body([{"role": "user", "content": [{"type": "text", "text": "hello"}]}])
        threads = [threading.Thread(target=_post, args=(proxy.port, body)) for body in bodies]
        for thread in threads:
            thread.start()
        time.sleep(0.3)
        waited = _post(proxy.port, text)
        for thread in threads:
            thread.join()
    finally:
        proxy.stop()
        server.shutdown()
        server.server_close()
    assert waited < 1


@pytest.mark.parametrize("field", ["messages", "input"])
def test_prime_fills_the_cache(field):
    data = b64(simple_pdf(2))
    block = ({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}
             if field == "messages" else
             {"type": "input_file", "filename": "a.pdf", "file_data": "data:application/pdf;base64," + data})
    item = {"role": "user", "content": [block]} if field == "messages" else {"type": "message", "role": "user",
                                                                             "content": [block]}
    RewriteEngine(MemoryStore(key=b"k" * 32), config=Config()).prime({"model": "m", field: [item]})
    assert tokens._PDF_PAGES[(len(data), hash(data))] == (2, True)


def test_priming_never_raises():
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=Config())
    for request in (None, [], {"messages": 5}, {"messages": [{"content": [{"type": "document", "source": 3}]}]},
                    {"input": [{"type": "input_file", "file_data": "data:application/pdf;base64,%%%"}]}):
        engine.prime(request)


def test_threads_waiting_on_the_same_pdf_parse_it_once(monkeypatch):
    """PR #248 round 1 (nit): the count went into the cache after the parse lock was released, so a thread
    waiting on that lock could parse the same PDF again."""
    calls, started = [], threading.Event()
    real = tokens.count_pages

    def slow(raw):
        calls.append(1)
        started.set()
        time.sleep(0.2)
        return real(raw)

    monkeypatch.setattr(tokens, "count_pages", slow)
    data = b64(simple_pdf(2))
    first = threading.Thread(target=pdf_page_count, args=(data,))
    first.start()
    started.wait(5)
    others = [threading.Thread(target=pdf_page_count, args=(data,)) for _ in range(5)]
    for thread in others:
        thread.start()
    for thread in [first, *others]:
        thread.join()
    assert len(calls) == 1 and pdf_page_count(data) == (2, True)


def test_a_request_with_300_pdfs_parses_each_once(monkeypatch):
    """#234 round 3: past 256 distinct PDFs an LRU missed on every access: 910 parses in one engine call."""
    calls = []
    real = tokens.count_pages
    monkeypatch.setattr(tokens, "count_pages", lambda raw: calls.append(1) or real(raw))
    messages = [{"role": "user", "content": [doc(simple_pdf(1, filler=100 + i)) for i in range(300)]}]
    for _ in range(3):
        estimate_message_tokens(messages)
    assert len(calls) == 300
