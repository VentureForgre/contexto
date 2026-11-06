"""D48: images and documents are priced by pixels and pages, and kept out of the bytes-per-token ratio.

Measured with count_tokens on claude-haiku-5-5 (QA, 2026-11-05): a 1440x900 PNG that Claude Code sends as
624,872 base64 characters costs 1,719 tokens (about w*h/750). On v0.5.8 the engine estimated it at about
178,000 tokens as a pasted image (3.5 bytes per token) and about 417,000 inside a tool_result (1.5 bytes
per token, how the Read tool returns screenshots); a 3-page PDF really 4,748 was estimated at 498,000.
The session tests use a synthetic upstream that charges text at 3.0 bytes per token plus w*h/750 per image.
"""

from __future__ import annotations

import base64
import http.client
import json
import math
import random
import struct
import threading
import time
import tracemalloc
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from contexto import tokens
from contexto.canonical import canonical_json
from contexto.codex import RESPONSES
from contexto.proxy import ContextoProxy
from contexto.rewrite import Config, RewriteEngine
from contexto.store import SessionStore
from contexto.store.memory import MemoryStore
from contexto.store.render import render_page
from contexto.tokens import (
    IMAGE_MAX_TOKENS,
    IMAGE_MIN_TOKENS,
    PDF_FLOOR_PAGE_TOKENS,
    PDF_PAGE_TOKENS,
    TokenEstimator,
    estimate_message_tokens,
    image_size,
    image_tokens,
    media_cost,
    pdf_page_count,
    pdf_pages,
    split_media,
)
from fixtures.make_pdfs import COUNTED_HIGH, EXPECTED

EP = "/v1/messages"
W, H = 1440, 900
REAL_IMAGE_TOKENS = math.ceil(W * H / 750)  # 1,728; count_tokens measured 1,719 for this size
CAP = 10 * REAL_IMAGE_TOKENS  # the QA bound: an order of magnitude over the real cost still passes


def png_bytes(width: int = W, height: int = H, payload: int = 468_000, seed: int = 1) -> bytes:
    """A PNG header with the given size plus incompressible payload: the base64 length of a real
    1440x900 screenshot with a photo panel (468 KB file, about 625 KB base64)."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    return b"\x89PNG\r\n\x1a\n" + chunk + random.Random(seed).randbytes(payload)


def png_b64(width: int = W, height: int = H, payload: int = 468_000, seed: int = 1) -> str:
    return base64.b64encode(png_bytes(width, height, payload, seed)).decode()


def jpeg_bytes(width: int, height: int, exif: int = 30_000) -> bytes:
    """SOI, an APP1 segment of ``exif`` bytes (split as JPEG requires), a DQT, then SOF2 with the size."""
    out = b"\xff\xd8"
    left = exif
    while left > 0:
        part = min(left, 65_000)
        out += b"\xff\xe1" + struct.pack(">H", part + 2) + bytes(part)
        left -= part
    out += b"\xff\xdb" + struct.pack(">H", 67) + bytes(65)
    out += b"\xff\xc2" + struct.pack(">HBHHB", 17, 8, height, width, 3) + bytes(9)
    return out + b"\xff\xda" + bytes(200_000)


def counted(data: str) -> tuple[int, bool]:
    """(pages, counted) of a base64 PDF."""
    return tuple(pdf_page_count(data)[:2])


def image_block(seed: int = 1, data: str | None = None, media_type: str = "image/png") -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                        "data": data if data is not None else png_b64(seed=seed)}}


def pdf_b64(pages: int, *, object_stream: bool = False, filler: int = 300_000) -> str:
    objs = b"".join(b"%d 0 obj << /Type /Page /Parent 2 0 R >> endobj\n" % (i + 3) for i in range(pages))
    if object_stream:
        packed = zlib.compress(objs)
        objs = b"9 0 obj << /Type /ObjStm /N %d /Filter /FlateDecode /Length %d >>\nstream\n" % (
            pages, len(packed)) + packed + b"\nendstream endobj\n"
    kids = b" ".join(b"%d 0 R" % (i + 3) for i in range(pages))
    body = b"%%PDF-1.5\n2 0 obj << /Type /Pages /Count %d /Kids [ %s ] >> endobj\n" % (pages, kids) + objs
    return base64.b64encode(body + random.Random(5).randbytes(filler)).decode()


# The estimate -------------------------------------------------------------------------------------

def test_pasted_image_block_is_not_priced_by_base64_bytes():
    messages = [{"role": "user", "content": [{"type": "text", "text": "what is wrong here?"}, image_block()]}]
    estimate = estimate_message_tokens(messages)
    assert estimate < CAP  # v0.5.8: about 178,000
    assert REAL_IMAGE_TOKENS <= estimate < REAL_IMAGE_TOKENS + 50


def test_read_tool_image_result_is_not_priced_by_base64_bytes():
    messages = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                              "content": [image_block()]}]}]
    estimate = estimate_message_tokens(messages)
    assert estimate < CAP  # v0.5.8: about 417,000
    assert REAL_IMAGE_TOKENS <= estimate < REAL_IMAGE_TOKENS + 50
    # The measured ratio prices the text around it; the image keeps its pixel cost.
    assert estimate_message_tokens(messages, 3.0) - estimate_message_tokens(messages) < 30


@pytest.mark.parametrize(("width", "height", "measured"), [(800, 600, 641), (1440, 900, 1_719),
                                                            (1920, 1080, 2_694), (2000, 1250, 3_243)])
def test_image_estimate_tracks_count_tokens(width, height, measured):
    estimate = estimate_message_tokens([{"role": "user", "content": [image_block(
        data=png_b64(width, height, payload=1_000))]}])
    assert measured * 0.99 <= estimate <= measured * 1.05


def test_image_dimensions_are_read_from_png_jpeg_gif_and_webp_headers():
    assert image_size(png_bytes(1440, 900, payload=10)) == (1440, 900)
    assert image_size(jpeg_bytes(1920, 1080)) == (1920, 1080)
    assert image_size(b"GIF89a" + struct.pack("<HH", 640, 480) + bytes(10)) == (640, 480)
    lossy = b"RIFF\0\0\0\0WEBPVP8 \0\0\0\0" + b"\x00\x00\x00\x9d\x01\x2a" + struct.pack("<HH", 1280, 720)
    assert image_size(lossy) == (1280, 720)
    bits = (1023 - 1) | ((767 - 1) << 14)
    lossless = b"RIFF\0\0\0\0WEBPVP8L\0\0\0\0\x2f" + bits.to_bytes(4, "little")
    assert image_size(lossless) == (1023, 767)
    extended = b"RIFF\0\0\0\0WEBPVP8X\0\0\0\0" + bytes(4) + (3999).to_bytes(3, "little") + (2999).to_bytes(3, "little")
    assert image_size(extended) == (4000, 3000)
    assert image_size(b"not an image") is None
    assert image_size(b"\xff\xd8\xff\xe1\x00") is None  # truncated JPEG


def test_jpeg_frame_after_a_large_exif_segment_is_found():
    data = base64.b64encode(jpeg_bytes(1920, 1080, exif=150_000)).decode()
    tokens = estimate_message_tokens([{"role": "user", "content": [image_block(data=data, media_type="image/jpeg")]}])
    assert tokens == image_tokens(1920, 1080) == 2_765


def test_large_images_follow_the_api_downscaling_and_cap():
    assert image_tokens(2576, 1449) == IMAGE_MAX_TOKENS  # 3.73 MP: capped at the documented maximum
    assert image_tokens(8000, 2000) == math.ceil(2576 * 644 / 750)  # long edge scaled to 2,576
    assert image_tokens(10, 10) == image_tokens(1, 2576) == IMAGE_MIN_TOKENS  # patch rounding is not modelled


@pytest.mark.parametrize("block", [
    {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}},
    {"type": "image", "source": {"type": "file", "file_id": "file_1"}},
    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA" * 50_000}},
    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "!!not base64!!"}},
    {"type": "image"},  # damaged: no source
])
def test_images_without_a_readable_size_get_the_largest_cost(block):
    assert estimate_message_tokens([{"role": "user", "content": [block]}]) == IMAGE_MAX_TOKENS


def test_document_block_is_not_priced_by_base64_bytes():
    # A 300 KB, 3-page PDF costs a few thousand tokens a page, not the 115,000 of v0.5.8.
    messages = [{"role": "user", "content": [{"type": "document", "source": {
        "type": "base64", "media_type": "application/pdf", "data": pdf_b64(3, filler=300_000)}}]}]
    assert estimate_message_tokens(messages) == 3 * PDF_PAGE_TOKENS < 40_000
    # The QA stand-in (random bytes after the header) has no readable pages: its count is a guess of one
    # page per 3 KB, which errs high, since the bound depends on it (reviewer A, PR #234).
    unreadable = base64.b64encode(b"%PDF-1.4\n" + random.Random(3).randbytes(300_000)).decode()
    assert counted(unreadable) == (101, False)


def test_pdf_pages_are_counted_from_page_objects_and_object_streams():
    assert pdf_pages(pdf_b64(3)) == 3
    assert pdf_pages(pdf_b64(4, object_stream=True)) == 4
    tree = base64.b64encode(b"%PDF-1.7\n1 0 obj << /Type /Pages /Count 12 /Kids [] >> endobj\n").decode()
    assert pdf_pages(tree) == 12
    block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64(3)}}
    assert estimate_message_tokens([{"role": "user", "content": [block]}]) == 3 * PDF_PAGE_TOKENS


def test_text_documents_and_url_documents():
    text = {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": "x" * 3_500}}
    assert 1_000 <= estimate_message_tokens([{"role": "user", "content": [text]}]) < 1_100  # measured as text
    url = {"type": "document", "source": {"type": "url", "url": "https://example.com/a.pdf"}}
    assert estimate_message_tokens([{"role": "user", "content": [url]}]) == 10 * PDF_PAGE_TOKENS


def test_split_media_copies_only_the_path_to_media():
    plain = {"type": "tool_result", "content": [{"type": "text", "text": "x"}]}
    assert split_media(plain) == (plain, []) and split_media(plain)[0] is plain
    image = image_block(data=png_b64(payload=10))
    mixed = {"type": "tool_result", "content": [{"type": "text", "text": "x"}, image]}
    stripped, found = split_media(mixed)
    assert found == [image] and stripped["content"] == [{"type": "text", "text": "x"}, None]
    assert mixed["content"][1] is image  # the input is not changed


def test_codex_input_image_is_priced_by_pixels():
    url = "data:image/png;base64," + png_b64()
    items = [{"type": "message", "role": "user",
              "content": [{"type": "input_text", "text": "what is wrong?"}, {"type": "input_image", "image_url": url}]}]
    tokens = RESPONSES.estimate_items(TokenEstimator(), items)
    assert REAL_IMAGE_TOKENS <= tokens < REAL_IMAGE_TOKENS + 100  # v0.5.8: about 178,000
    pdf = {"type": "input_file", "filename": "a.pdf", "file_data": "data:application/pdf;base64," + pdf_b64(2)}
    items = [{"type": "message", "role": "user", "content": [pdf]}]
    assert 2 * PDF_PAGE_TOKENS <= RESPONSES.estimate_items(TokenEstimator(), items) < 2 * PDF_PAGE_TOKENS + 50


# The session -------------------------------------------------------------------------------------

def _engine(tmp_path, **config):
    return RewriteEngine(SessionStore(tmp_path, "img"), config=Config(**config))


def _real_usage(body: dict) -> int:
    """What the synthetic upstream charges: text at 3.0 bytes per token plus w*h/750 per image."""
    raw = canonical_json(body)
    stripped, images = split_media(body["messages"])
    image_bytes = sum(len(canonical_json(b)) for b in images)
    return math.ceil((len(raw) - image_bytes) / 3.0) + REAL_IMAGE_TOKENS * len(images)


HEAD = {"model": "claude-haiku-5-5", "max_tokens": 32000, "stream": True,
        "system": [{"type": "text", "text": "You are Claude Code. " + "s" * 50_000}], "tools": []}


def _send(eng, messages):
    req = {**HEAD, "messages": list(messages)}
    upstream, _, events = eng(req, {}, EP)
    real = _real_usage(upstream)
    eng.observe_usage(EP, {"input_tokens": real, "output_tokens": 10}, req)
    request = next(e for e in events if e["event"] == "request")
    return upstream, events, request["estimate"], real


def test_one_image_does_not_poison_the_measured_ratio(tmp_path):
    eng = _engine(tmp_path)
    msgs = [{"role": "user", "content": [{"type": "text", "text": "look"}, image_block()]}]
    _send(eng, msgs)
    msgs += [{"role": "assistant", "content": [{"type": "text", "text": "I see it."}]},
             {"role": "user", "content": [{"type": "text", "text": "thanks"}]}]
    _send(eng, msgs)
    # The true text ratio is 3.0. v0.5.8: about 36.5 bytes per token, so later text was under-estimated 12x.
    # The ratio takes off the image's floor cost (1,568), so it errs low: text is over-, never under-counted.
    assert 2.9 <= eng.estimator.measured_bytes_per_token <= 3.0


def test_text_after_screenshots_is_estimated_at_the_text_ratio(tmp_path):
    """The QA stub run: two Read screenshots, then a large Read in a tool loop. On v0.5.8 a 61,000-token
    estimate was really 81,000 against a 60,000 bound."""
    eng = _engine(tmp_path, bound=60_000)
    msgs: list[dict] = [{"role": "user", "content": [{"type": "text", "text": "check the screenshots"}]}]
    _send(eng, msgs)
    for i in (1, 2):
        msgs += [{"role": "assistant", "content": [{"type": "tool_use", "id": f"toolu_{i}", "name": "Read",
                                                    "input": {"file_path": f"/tmp/s{i}.png"}}]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"toolu_{i}",
                                               "content": [image_block(seed=i)]}]}]
        _send(eng, msgs)
    rng = random.Random(4)
    text = " ".join(rng.choices(["def", "return", "self", "value", "x", "=", "(", ")"], k=30_000))
    msgs += [{"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_3", "name": "Read",
                                                "input": {"file_path": "/tmp/big.py"}}]},
             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_3", "content": text}]}]
    _, _, estimate, real = _send(eng, msgs)
    assert real * 0.99 <= estimate <= real * 1.05


def test_pasted_screenshot_does_not_page_the_window_at_the_default_bound(tmp_path):
    eng = _engine(tmp_path)
    rng = random.Random(2)
    msgs: list[dict] = []
    for t in range(6):  # about 40,000 real tokens of recent conversation, well inside 150,000
        msgs.append({"role": "user", "content": [{"type": "text", "text": f"turn {t} " + " ".join(
            rng.choices(["alpha", "beta", "gamma"], k=3000))}]})
        _send(eng, msgs)
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": "ok " * 300}]})
    msgs.append({"role": "user", "content": [{"type": "text", "text": "what is wrong in this screenshot?"},
                                             image_block(seed=9)]})
    upstream, events, estimate, real = _send(eng, msgs)
    names = [e["event"] for e in events]
    # The real request is about 60,000 tokens. v0.5.8: window_minimal + bound_too_small, 1 message upstream.
    assert not {"page_out", "rotation", "window_minimal", "bound_too_small", "turn_room_low"} & set(names)
    assert len(upstream["messages"]) == len(msgs)
    assert real * 0.99 <= estimate <= real * 1.05


def test_restart_after_a_screenshot_turn_keeps_the_turns(tmp_path):
    """A new engine on the session (a proxy restart or --resume) estimates the whole history at once."""
    eng = _engine(tmp_path)
    msgs = [{"role": "user", "content": [{"type": "text", "text": "look"}, image_block()]},
            {"role": "assistant", "content": [{"type": "text", "text": "I see it."}]},
            {"role": "user", "content": [{"type": "text", "text": "and this one"}, image_block(seed=2)]}]
    _send(eng, msgs[:1])
    fresh = _engine(tmp_path)
    upstream, events, estimate, real = _send(fresh, msgs)
    assert not {"page_out", "rotation", "window_minimal", "bound_too_small"} & {e["event"] for e in events}
    assert len(upstream["messages"]) == 3 and estimate < 2 * real


def test_a_paged_screenshot_turn_still_renders_as_image_omitted(tmp_path):
    """Paging is unchanged: the page keeps the image as sent, and recall renders it as ``[image omitted]``."""
    eng = _engine(tmp_path, bound=40_000, headroom_floor=5_000, min_turn_room=1_000)
    msgs: list[dict] = [{"role": "user", "content": [{"type": "text", "text": "screenshot turn"}, image_block()]}]
    _send(eng, msgs)
    rng = random.Random(6)
    for t in range(8):
        msgs += [{"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
                 {"role": "user", "content": [{"type": "text", "text": f"turn {t} " + " ".join(
                     rng.choices(["alpha", "beta", "gamma"], k=4000))}]}]
        upstream, events, _, _ = _send(eng, msgs)
    assert any(e["event"] == "page_out" for e in events) or eng.store.page_ids()
    first = min((eng.store.get_page(i) for i in eng.store.page_ids()), key=lambda page: page["turn"])
    assert first["messages"][0]["content"][1] == msgs[0]["content"][1]
    assert "[image omitted]" in render_page(first) and "screenshot turn" in render_page(first)
    assert first["tokens"] < CAP


def test_an_image_shaped_tool_input_is_text():
    """Media is looked for only where the API takes it; a tool's input is JSON text the model wrote."""
    shaped = image_block(data=png_b64(payload=10))
    use = {"type": "tool_use", "id": "toolu_1", "name": "Write", "input": {"content": [shaped]}}
    assert split_media([{"role": "assistant", "content": [use]}])[1] == []
    assert split_media({"content": [{"content": [{"content": [shaped]}]}]}, depth=9)[1] == []  # beyond the depth


def test_codex_function_call_output_image_is_media():
    url = "data:image/png;base64," + png_b64()
    items = [{"type": "function_call_output", "call_id": "c1",
              "output": [{"type": "input_text", "text": "screenshot"}, {"type": "input_image", "image_url": url}]}]
    assert REAL_IMAGE_TOKENS <= RESPONSES.estimate_items(TokenEstimator(), items) < REAL_IMAGE_TOKENS + 100


def test_a_guessed_page_count_does_not_raise_the_ratio_floor():
    guessed = base64.b64encode(b"%PDF-1.4\n" + random.Random(3).randbytes(300_000)).decode()
    assert counted(guessed) == (101, False) and counted(pdf_b64(3)) == (3, True)
    doc = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": guessed}}
    assert media_cost([doc]) == (0, 101 * PDF_PAGE_TOKENS, PDF_FLOOR_PAGE_TOKENS)
    read = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64(3)}}
    assert media_cost([read]).floor == 3 * PDF_FLOOR_PAGE_TOKENS


# Reviewer A, PR #234: hostile and unusual PDFs ----------------------------------------------------

def _doc(raw: bytes) -> dict:
    return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                           "data": base64.b64encode(raw).decode()}}


def _timed_count(raw: bytes) -> tuple[tuple[int, bool], float, int]:
    """pdf_page_count of ``raw``: its result, seconds, and peak traced memory in bytes."""
    data = base64.b64encode(raw).decode()
    tracemalloc.start()
    started = time.perf_counter()
    try:
        result = counted(data)
        return result, time.perf_counter() - started, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def _object_stream(body: bytes, extra: bytes = b"") -> bytes:
    packed = zlib.compress(body)
    return (b"9 0 obj << /Type /ObjStm /N 1 /Filter /FlateDecode " + extra + b" /Length %d >>\nstream\n"
            % len(packed) + packed + b"\nendstream endobj\n")


def test_a_count_with_thousands_of_digits_does_not_raise(tmp_path):
    raw = b"%PDF-1.7\n<</Type/Pages/Count " + b"9" * 5000 + b">>"
    assert counted(base64.b64encode(raw).decode()) == (2, False)  # no count read: one page per 3 KB
    eng = _engine(tmp_path)
    req = {**HEAD, "messages": [{"role": "user", "content": [{"type": "text", "text": "read this"}, _doc(raw)]}]}
    upstream, _, events = eng(req, {}, EP)  # v0.5.8 head: ValueError, so the proxy sent every request unwindowed
    assert upstream["messages"] == req["messages"] and any(e["event"] == "request" for e in events)


def test_media_costs_never_raise():
    odd = [{"type": "image", "source": {"type": "base64", "data": "/9j/" + "A" * 10}},  # a JPEG cut short
           {"type": "image", "source": {"type": "base64", "data": 7}},
           {"type": "image", "source": "x"}, {"type": "input_image", "image_url": {"url": 5}},
           {"type": "document", "source": {"type": "base64", "data": "%%%"}}, {"type": "input_file"}]
    assert media_cost(odd).tokens > 0


def test_a_quadratic_count_pattern_is_linear():
    result, seconds, _ = _timed_count(b"%PDF-1.7\n" + b"/Count 1 " * 80_000)  # 720 KB: 130 s on the head
    assert seconds < 2 and result == (241, False)  # no page objects or tree: a guess of one page per 3 KB


def test_zlib_bombs_are_bounded_in_memory_and_time():
    bomb = _object_stream(bytes(4 << 20))  # 4 MB of zeros in about 4 KB
    result, seconds, peak = _timed_count(b"%PDF-1.7\n" + bomb * 50)  # 204 MB peak on the head
    assert seconds < 2 and peak < 64 << 20
    assert result == (600, False)  # the inflate budget ran out and no page tree was read: the API's maximum


def test_too_many_object_streams_is_a_guess():
    page = b"3 0 obj << /Type /Page >> endobj "
    raw = b"%PDF-1.7\n" + _object_stream(page) * 200
    assert counted(base64.b64encode(raw).decode()) == (600, False)  # 128 read, the rest unknown
    with_tree = raw + b"2 0 obj << /Type /Pages /Count 200 >> endobj"
    assert counted(base64.b64encode(with_tree).decode()) == (200, True)  # the tree's own total


def test_line_wrapped_base64_is_counted():
    clean = pdf_b64(100, filler=300_028)  # a length whose wrapped form failed to decode at 2ae9ff4: (1, False)
    wrapped = "\n".join(clean[i:i + 76] for i in range(0, len(clean), 76))
    assert counted(clean) == counted(wrapped) == (100, True)
    assert counted("\r\n".join(clean[i:i + 64] for i in range(0, len(clean), 64))) == (100, True)
    image = "\n".join(png_b64(payload=200)[i:i + 76] for i in range(0, 300, 76))
    assert estimate_message_tokens([{"role": "user", "content": [image_block(data=image)]}]) == REAL_IMAGE_TOKENS


def test_object_stream_with_a_nested_dictionary_is_read():
    pages = b"".join(b"%d 0 obj << /Type /Page /Parent 2 0 R >> endobj " % (i + 3) for i in range(100))
    tree = b"2 0 obj << /Type /Pages /Count 100 /Kids [] >> endobj "
    raw = b"%PDF-1.5\n" + _object_stream(tree + pages, b"/DecodeParms << /Columns 4 >>")
    assert counted(base64.b64encode(raw).decode()) == (100, True)  # the head: (1, False)


@pytest.mark.parametrize("extra", [b"/DecodeParms << /Predictor 12 /Columns 4 >>", b"/Filter2 /LZWDecode"])
def test_an_unreadable_object_stream_errs_high(extra):
    pages = b"".join(b"%d 0 obj << /Type /Page >> endobj " % (i + 3) for i in range(100))
    raw = b"%PDF-1.5\n" + _object_stream(pages, extra)
    if b"LZW" in extra:
        raw = raw.replace(b"/Filter /FlateDecode", b"/Filter /LZWDecode")
    assert counted(base64.b64encode(raw).decode()) == (600, False)


# Reviewer A, PR #234: the Codex floor and the measured head -------------------------------------

def test_codex_media_has_no_floor_so_the_ratio_never_rises(tmp_path):
    """OpenAI's tile price (1,105 tokens for 1920x1080 at high detail, 85 at low) is under Claude's floor,
    so Codex requests take no media cost off real usage: the ratio can only err toward over-counting."""
    eng = RewriteEngine(MemoryStore(key=b"k" * 32), config=Config())
    shot = "data:image/png;base64," + png_b64(1920, 1080, payload=1_000)
    items = [{"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "d" * 2_000}]},
             {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "x" * 45_000}]}]
    image = {"type": "input_image", "image_url": shot}
    items += [{"type": "function_call_output", "call_id": f"c{i}", "output": [image]} for i in range(10)]
    req = {"model": "gpt-6-astra", "stream": True, "input": items}
    upstream, _, _ = eng(req, {}, "/v1/responses")
    _, found = split_media(upstream["input"])
    text_bytes = len(canonical_json(upstream).encode()) - sum(len(canonical_json(b).encode()) for b in found)
    eng.observe_usage("/v1/responses", {"input_tokens": math.ceil(text_bytes / 3.0) + 1_105 * 10}, req)
    ratio = eng.estimator.measured_bytes_per_token  # Claude's floor here: 4.31, text 30% low
    assert ratio is None or ratio <= 3.0  # 10 images' cost is too uncertain to set a first ratio from


def test_a_document_in_the_first_request_does_not_zero_the_head(tmp_path):
    """The head is the first request's usage minus its window, with the window's media at their floor."""
    eng = _engine(tmp_path)
    req = {**HEAD, "messages": [{"role": "user", "content": [{"type": "text", "text": "summarise"},
                                                             _doc(base64.b64decode(pdf_b64(20)))]}]}
    upstream, _, _ = eng(req, {}, EP)
    stripped, _ = split_media(upstream)
    true_head = math.ceil(len(canonical_json({**stripped, "messages": []})) / 3.0)
    eng.observe_usage(EP, {"input_tokens": _real_usage(upstream) + 20 * 1_583, "output_tokens": 10}, req)
    state = next(iter(eng.states.values()))
    assert true_head <= state.head_tokens <= true_head + 20 * 1_583  # the head: 0


# Reviewer A, PR #234, round 2: real producers, speed, the cache, and a floor that errs low -----------

FIXTURES = Path(__file__).parent / "fixtures" / "pdf"


def _fixture(name: str) -> str:
    return base64.b64encode((FIXTURES / name).read_bytes()).decode()


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_real_producers_count_exactly(name):
    """reportlab, fpdf2, pikepdf (libqpdf) object streams with and without linearization, macOS Quartz
    (cupsfilter, sips), incremental updates that add or rewrite a page, and PDFlib's object stream with
    an indirect /Length (round 2: /Length 15 0 R was read as 1 byte, so the file counted 600 pages)."""
    assert pdf_page_count(_fixture(name)) == (EXPECTED[name], True, EXPECTED[name])


def test_a_pdflib_file_does_not_page_the_window(tmp_path):
    eng = _engine(tmp_path)
    msgs: list[dict] = []
    for t in range(3):
        msgs.append({"role": "user", "content": [{"type": "text", "text": f"turn {t} " + "words " * 500}]})
        _send(eng, msgs)
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
    pdf = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                          "data": _fixture("pdflib-indirect-length-1.pdf")}}
    msgs.append({"role": "user", "content": [{"type": "text", "text": "read this"}, pdf]})
    upstream, events, estimate, real = _send(eng, msgs)
    # 23c42ea: page_out, rotation, window_minimal, bound_too_small, turn_room_low; estimate 2,763,637
    assert not {"page_out", "rotation", "window_minimal", "bound_too_small"} & {e["event"] for e in events}
    assert estimate < real + 2 * PDF_PAGE_TOKENS


def test_the_floor_takes_the_lower_of_the_two_counts():
    pages = b"".join(b"%d 0 obj << /Type /Page /Parent 2 0 R >> endobj " % (i + 3) for i in range(20))
    raw = b"%PDF-1.4\n2 0 obj << /Type /Pages /Count 3 /Kids [ 3 0 R 4 0 R 5 0 R 6 0 R ] >> endobj " + pages
    assert pdf_page_count(base64.b64encode(raw).decode()) == (20, True, 3)  # estimate high, floor low
    only_pages = b"%PDF-1.4\n" + pages
    assert pdf_page_count(base64.b64encode(only_pages).decode()) == (20, True, 1)  # one claim: floor one page


def test_a_crafted_page_count_does_not_raise_the_ratio(tmp_path):
    """A 1-page PDF (the upstream charges 1,583) that claims 40 pages in both its page objects and its tree.
    At 23c42ea the ratio went from 3.0 to 5.73 and a later Read was estimated 11% low."""
    eng = _engine(tmp_path)
    pages = b"".join(b"%d 0 obj << /Type /Page /Parent 2 0 R >> endobj " % (i + 3) for i in range(40))
    raw = b"%PDF-1.4\n2 0 obj << /Type /Pages /Count 40 /Kids [] >> endobj " + pages
    doc = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                          "data": base64.b64encode(raw).decode()}}
    msgs: list[dict] = [{"role": "user", "content": [{"type": "text", "text": "read this"}, doc]}]
    req = {**HEAD, "messages": list(msgs)}
    upstream, _, _ = eng(req, {}, EP)
    eng.observe_usage(EP, {"input_tokens": _real_usage(upstream) + 1_583, "output_tokens": 10}, req)
    ratio = eng.estimator.measured_bytes_per_token
    assert ratio is None or ratio <= 3.0  # 23c42ea: 10.24
    rng = random.Random(7)
    text = " ".join(rng.choices(["def", "return", "self", "value", "x", "=", "(", ")"], k=30_000))
    msgs += [{"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "Read",
                                                "input": {"file_path": "big.py"}}]},
             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": text}]}]
    req = {**HEAD, "messages": list(msgs)}
    upstream, _, events = eng(req, {}, EP)
    estimate = next(e for e in events if e["event"] == "request")["estimate"]
    assert estimate >= _real_usage(upstream) + 1_583  # never under the real cost


def _inflating(count: int, body: bytes) -> bytes:
    stream = _object_stream(body)
    return b"%PDF-1.7\n" + stream * count


@pytest.mark.parametrize("case", ["inflate", "cap"])
def test_crafted_pdfs_parse_in_well_under_a_second(case):
    """Round 2: 50 KB that inflates to 5 streams of /Count took 2.2 s, and the 32 MB worst case 6 s,
    under the proxy's one transform lock."""
    inflate = _inflating(5, b"/Count 1 " * (3_990_000 // 9))
    raw = inflate if case == "inflate" else inflate + b"/Count 1 " * (((24 << 20) - len(inflate)) // 9)
    result, seconds, _ = _timed_count(raw)
    assert seconds < 1 and result[:2] == (600, False)


def test_the_page_cache_keeps_more_than_64_pdfs(monkeypatch):
    """Round 2: with 70 PDFs in the window the 64-entry FIFO parsed every PDF 4 times a request."""
    calls = []
    real = tokens._count_pdf_pages
    monkeypatch.setattr(tokens, "_count_pdf_pages", lambda data: calls.append(1) or real(data))
    docs = [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                            "data": pdf_b64(1, filler=1_000 + i)}} for i in range(70)]
    messages = [{"role": "user", "content": docs}]
    tokens._PDF_PAGES.clear()
    for _ in range(4):
        estimate_message_tokens(messages)
    assert len(calls) == 70


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


def test_the_proxy_counts_pdfs_before_it_takes_the_transform_lock(monkeypatch):
    """Round 3: ``prime`` cached nothing (it passed a list of lists, which ``split_media`` skips), so every PDF
    was still parsed under the lock every session waits on. Every parse must run with the lock free, and the
    cache must hold the request's PDFs when the transform starts."""
    parses: list[bool] = []
    real = tokens._count_pdf_pages

    def counting(data):
        parses.append(proxy.transform_lock.locked())
        return real(data)

    monkeypatch.setattr(tokens, "_count_pdf_pages", counting)
    seen: list[tuple] = []
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=Config())

    class Spy:
        preserves_input = True

        def prime(self, request):
            assert not proxy.transform_lock.locked()
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
        tokens._PDF_PAGES.clear()
        docs = [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": pdf_b64(3, filler=2_024 + i)}} for i in range(2)]
        pasted = {"role": "user", "content": [docs[0]]}
        read = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": [docs[1]]}]}
        messages = [pasted, {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "Read",
                                                               "input": {}}]}, read]
        body = json.dumps({**HEAD, "stream": False, "messages": messages}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)
        conn.request("POST", EP, body=body, headers={"Content-Type": "application/json", "x-api-key": "synthetic"})
        assert conn.getresponse().status == 200
        conn.close()
    finally:
        proxy.stop()
        server.shutdown()
        server.server_close()
    assert seen == [("primed", 2), ("transform", True, 2)]  # both PDFs counted before the lock, none after
    assert parses == [False, False]


@pytest.mark.parametrize("field", ["messages", "input"])
def test_prime_fills_the_cache(field):
    data = pdf_b64(2, filler=3_000)
    block = ({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}
             if field == "messages" else
             {"type": "input_file", "filename": "a.pdf", "file_data": "data:application/pdf;base64," + data})
    item = {"role": "user", "content": [block]} if field == "messages" else {"type": "message", "role": "user",
                                                                             "content": [block]}
    tokens._PDF_PAGES.clear()
    RewriteEngine(MemoryStore(key=b"k" * 32), config=Config()).prime({"model": "m", field: [item]})
    assert (len(data), hash(data)) in tokens._PDF_PAGES  # d95e5df: empty


def test_priming_never_raises():
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=Config())
    for request in (None, [], {"messages": 5}, {"messages": [{"content": [{"type": "document", "source": 3}]}]},
                    {"input": [{"type": "input_file", "file_data": "data:application/pdf;base64,%%%"}]}):
        engine.prime(request)


# Reviewer A, PR #234, round 3: per-PDF caps, the cache past 256 PDFs, and a floor a file cannot raise -------

def _streams(bodies: list[bytes]) -> bytes:
    return b"%PDF-1.7\n" + b"".join(_object_stream(body).replace(b"9 0 obj", b"%d 0 obj" % (10 + i))
                                    for i, body in enumerate(bodies))


def _stored_streams(bodies: list[bytes]) -> bytes:
    """Object streams stored without a filter, as PyMuPDF writes them."""
    header = b"%d 0 obj << /Type /ObjStm /N 1 /First 0 /Length %d >>\nstream\n"
    return b"%PDF-1.7\n" + b"".join(header % (10 + i, len(body)) + body + b"\nendstream endobj\n"
                                    for i, body in enumerate(bodies))


TREE_MARKERS = b"/Type/Pages " * 999 + b"/Count " * 1_300
PAGE_MARKERS = b"/Type/Page " * 9_999


@pytest.mark.parametrize("bodies", [[TREE_MARKERS] * 128, [PAGE_MARKERS] * 128,
                                    [TREE_MARKERS, PAGE_MARKERS] * 64], ids=["tree", "pages", "mixed"])
@pytest.mark.parametrize("stored", [False, True], ids=["flate", "stored"])
def test_caps_hold_per_pdf_across_object_streams(bodies, stored):
    """Round 3: the caps restarted in every object stream, so 128 streams multiplied them: a 30 KB PDF took
    1.8 to 2.5 s (d95e5df), and ten of them held another session's text request for 18.6 s."""
    result, seconds, _ = _timed_count(_stored_streams(bodies) if stored else _streams(bodies))
    assert seconds < 0.5 and result == (600, False)


def test_a_request_with_300_pdfs_parses_each_once(monkeypatch, tmp_path):
    """Round 3: past 256 distinct PDFs the LRU missed on every access: 910 parses in one engine call."""
    calls = []
    real = tokens._count_pdf_pages
    monkeypatch.setattr(tokens, "_count_pdf_pages", lambda data: calls.append(1) or real(data))
    tokens._PDF_PAGES.clear()
    eng = _engine(tmp_path, bound=10_000_000)
    msgs: list[dict] = []
    for turn in range(30):
        docs = [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": pdf_b64(1, filler=500 + 10 * turn + i)}} for i in range(10)]
        msgs.append({"role": "user", "content": [{"type": "text", "text": f"turn {turn}"}, *docs]})
        _send(eng, msgs)
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
    assert len(calls) == 300


def _claiming(drawn: int, claimed: int) -> bytes:
    """A PDF whose page tree draws ``drawn`` pages but that holds ``claimed`` page objects and /Count."""
    kids = b" ".join(b"%d 0 R" % (i + 3) for i in range(drawn))
    pages = b"".join(b"%d 0 obj << /Type /Page /Parent 2 0 R >> endobj " % (i + 3) for i in range(claimed))
    return b"%%PDF-1.4\n2 0 obj << /Type /Pages /Count %d /Kids [ %s ] >> endobj " % (claimed, kids) + pages


def test_the_floor_counts_only_pages_the_tree_draws():
    assert pdf_page_count(base64.b64encode(_claiming(1, 8)).decode()) == (8, True, 1)  # estimate high, floor low
    assert pdf_page_count(base64.b64encode(_claiming(8, 8)).decode()) == (8, True, 8)


def test_a_crafted_claim_in_a_dense_session_is_not_under_counted(tmp_path):
    """Round 3: text at a true 2.0 bytes a token sits below the 3.5 ceiling, so a 1-page PDF claiming 8 pages
    lifted the ratio to 2.34 and the next request was estimated 4% low. The floor now counts drawn pages."""
    eng = _engine(tmp_path)
    doc = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                          "data": base64.b64encode(_claiming(1, 8)).decode()}}

    def dense_usage(body: dict) -> int:  # every byte outside media at 2.0 bytes a token, the PDF's 1 page at 1,583
        stripped, found = split_media(body["messages"])
        return math.ceil(len(canonical_json({**body, "messages": stripped})) / 2.0) + 1_583 * len(found)

    rng = random.Random(8)
    msgs: list[dict] = [{"role": "user", "content": [{"type": "text", "text": "read"}, doc]}]
    for i in range(4):
        req = {**HEAD, "messages": list(msgs)}
        upstream, _, events = eng(req, {}, EP)
        estimate = next(e for e in events if e["event"] == "request")["estimate"]
        real = dense_usage(upstream)
        if i:
            assert estimate >= real * 0.99
        eng.observe_usage(EP, {"input_tokens": real, "output_tokens": 10}, req)
        blob = json.dumps([rng.randrange(10**6) for _ in range(3_000)])
        msgs += [{"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {}}]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": blob}]}]
    ratio = eng.estimator.measured_bytes_per_token
    assert ratio is None or ratio <= 2.0 * 1.01  # d95e5df: 2.35


# Reviewer B, PR #234, round 3: a dense PDF must not drag the ratio down ------------------------------------

def _dense_session(tmp_path, *, paste_first: bool, pages: int = 20) -> list[float | None]:
    """Text at 3.0 bytes a token, a PDF at 4,568 tokens a page (3,000 text plus a page image), read in a tool
    loop after a text turn or pasted in the first request, then 12 text turns with Reads. The ratio after
    each turn."""
    eng = _engine(tmp_path)
    doc = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                          "data": pdf_b64(pages, filler=pages * 20_000)}}
    rng = random.Random(9)
    msgs: list[dict] = []
    ratios: list[float | None] = []

    def send() -> None:
        req = {**HEAD, "messages": list(msgs)}
        upstream, _, events = eng(req, {}, EP)
        stripped, found = split_media(upstream["messages"])
        real = math.ceil(len(canonical_json({**upstream, "messages": stripped})) / 3.0) + pages * 4_568 * len(found)
        estimate = next(e for e in events if e["event"] == "request")["estimate"]
        if len(msgs) > 1:  # the first request is estimated by the seeds, as in any session
            assert estimate >= real * 0.97  # never meaningfully under
        eng.observe_usage(EP, {"input_tokens": real, "output_tokens": 10}, req)

    for turn in range(14):
        first = [{"type": "text", "text": f"turn {turn} " + "talk " * 400}]
        if paste_first and turn == 0:
            first.append(doc)
        msgs.append({"role": "user", "content": first})
        send()
        for r in range(2):
            msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"t{turn}_{r}", "name": "Read",
                                                           "input": {}}]})
            content = ([doc] if not paste_first and turn == 1 and r == 0
                       else " ".join(rng.choices(["def", "x", "=", "(", ")", "return"], k=2_500)))
            msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{turn}_{r}",
                                                      "content": content}]})
            send()
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
        ratios.append(eng.estimator.measured_bytes_per_token)
    return ratios


def test_a_dense_pdf_read_mid_session_keeps_the_ratio(tmp_path):
    """d95e5df took 1,000 a page off real usage for a PDF that cost 4,568: the ratio fell from 3.0 to 1.95 (20
    pages) or 0.81 (100 pages) and took about 30 turns to recover, so the session kept less context."""
    ratios = _dense_session(tmp_path, paste_first=False)
    assert all(r is not None and 2.85 <= r <= 3.0 for r in ratios)


def test_a_dense_pdf_in_the_first_request_does_not_set_a_low_first_ratio(tmp_path):
    """Before a ratio exists the PDF's cost (1,000 to 4,568 a page) is too uncertain to learn from; d95e5df
    learned 0.82, and 1.65 ten turns later."""
    ratios = _dense_session(tmp_path, paste_first=True, pages=10)
    assert all(r is None or 2.7 <= r <= 3.0 for r in ratios) and ratios[-1] is not None


# Reviewer A, PR #234, round 4: a floor that runs high, drift, and a PDF pasted first ------------------------

def _orphan_node_pdf() -> bytes:
    """One drawn page, plus a page-tree node no catalog reaches that lists 40 pages (pikepdf ignores it)."""
    pages = b"".join(b"%d 0 obj << /Type /Page /Parent 50 0 R >> endobj " % n for n in range(10, 50))
    orphan = b"50 0 obj << /Type /Pages /Count 40 /Kids [ %s ] >> endobj " % b" ".join(
        b"%d 0 R" % n for n in range(10, 50))
    return (b"%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj 2 0 obj << /Type /Pages /Count 1 "
            b"/Kids [ 3 0 R ] >> endobj 3 0 obj << /Type /Page /Parent 2 0 R >> endobj " + orphan + pages)


def _pdf_session(tmp_path, raw: bytes, cost: int, *, paste_first: bool = False, dense_after: int | None = None,
                 turns: int = 7, read_words: int = 3_000, bound: int = 150_000) -> dict:
    """Text at 3.0 bytes a token (JSON tool output at 2.0 from turn ``dense_after``), the PDF costing ``cost``
    in all, read on turn 2 or pasted in the first request, then text turns with Reads and a tool loop that
    runs until a request crosses the bound; with no ``raw``, the same session without the PDF. Returns the
    ratios after each request and any request whose real usage crossed the bound without a pre-send
    over_bound."""
    eng = _engine(tmp_path, bound=bound)
    doc = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                          "data": base64.b64encode(raw).decode()}}
    rng = random.Random(11)
    msgs: list[dict] = []
    out: dict = {"ratios": [], "unflagged": [], "estimates": []}

    def usage(upstream: dict) -> int:
        stripped, found = split_media(upstream["messages"])
        dense = sum(len(json.dumps(b["content"]).encode()) for m in stripped if isinstance(m.get("content"), list)
                    for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"
                    and isinstance(b.get("content"), str) and b["content"].startswith("["))
        text = len(canonical_json({**upstream, "messages": stripped})) - dense
        return math.ceil(text / 3.0 + dense / 2.0) + cost * len(found)

    def send() -> int:
        req = {**HEAD, "messages": list(msgs)}
        upstream, _, events = eng(req, {}, EP)
        real = usage(upstream)
        flagged = any(e["event"] == "over_bound" for e in events)
        if real > bound and not flagged:
            out["unflagged"].append(real)
        out["estimates"].append((next(e for e in events if e["event"] == "request")["estimate"], real))
        eng.observe_usage(EP, {"input_tokens": real, "output_tokens": 10}, req)
        out["ratios"].append(eng.estimator.measured_bytes_per_token)
        return real

    def tool_output(turn: int) -> str:
        if dense_after is not None and turn >= dense_after:
            return json.dumps([rng.randrange(10**6) for _ in range(read_words // 2)])
        return " ".join(rng.choices(["def", "x", "=", "(", ")", "return"], k=read_words))

    for turn in range(turns):
        first = [{"type": "text", "text": f"turn {turn} " + "talk " * 300}]
        if raw and paste_first and turn == 0:
            first.append(doc)
        msgs.append({"role": "user", "content": first})
        send()
        rounds = 2 if turn < turns - 1 else 400  # the last turn's tool loop runs until it crosses the bound
        for r in range(rounds):
            msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"t{turn}_{r}", "name": "Read",
                                                           "input": {}}]})
            content = [doc] if (raw and not paste_first and turn == 2 and r == 0) else tool_output(turn)
            msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{turn}_{r}",
                                                      "content": content}]})
            if send() > bound:
                break
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
    return out


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.mark.parametrize(("pdf", "cost"), [
    ("deleted", 10 * 1_583), ("deleted", 10 * 2_500), ("orphan", 1_583), ("orphan", 2_500), ("cheap", 20 * 300)],
    ids=["deleted-1583", "deleted-2500", "orphan-1583", "orphan-2500", "cheap-pages"])
def test_a_floor_that_runs_high_never_raises_the_ratio(tmp_path, pdf, cost):
    """Round 4: once a ratio was measured, a floor above the PDF's real cost was booked as sparse text: the
    ratio rose to 3.75-5.06 (true 3.0) and requests crossed the bound unflagged. Files: a PyMuPDF save that
    deleted 30 of 40 pages incrementally (the superseded tree still lists 40), a crafted orphan page-tree
    node, and pages cheaper than the 1,000 floor. Since round 5 the first two count right (the catalog's
    tree, by latest definitions), and the cheap pages still floor high."""
    raw = {"deleted": _fixture_bytes("pymupdf-incremental-deleted-40-to-10.pdf"), "orphan": _orphan_node_pdf(),
           "cheap": base64.b64decode(pdf_b64(20, filler=20_000))}[pdf]
    result = _pdf_session(tmp_path, raw, cost)
    assert max(r for r in result["ratios"] if r is not None) <= 3.0 * 1.005
    assert result["unflagged"] == []


def test_drift_to_denser_text_is_learned_with_a_pdf_in_view(tmp_path):
    """Round 4: with a 20-page PDF in the window, the floor-to-estimate slack absorbed the move to JSON tool
    output at 2.0 bytes a token, so the ratio held at 3.00 while the control fell. What a request appends
    to the last one, with the same media, is now learned as text."""
    raw = base64.b64decode(pdf_b64(20, filler=20_000))
    with_pdf = _pdf_session(tmp_path / "pdf", raw, 20 * 1_583, dense_after=3, turns=6, bound=400_000)
    control = _pdf_session(tmp_path / "control", b"", 0, dense_after=3, turns=6, bound=400_000)
    assert with_pdf["unflagged"] == [] and control["unflagged"] == []
    assert with_pdf["ratios"][-1] <= 2.5  # 134f939: 2.78 (control 2.20; each appended byte is learned once)

    def worst(result: dict) -> float:
        return min(estimate / real for estimate, real in result["estimates"][1:])

    assert worst(with_pdf) >= worst(control) - 0.01  # estimates stay as close to real as the control's


def test_a_pdf_pasted_first_learns_the_ratio_by_the_third_request(tmp_path):
    """Round 4: the 10% gate left the seeds in use for 9 to 51 requests while a PDF pasted first stayed in
    view; two requests with the same PDF now give the ratio from their difference."""
    raw = base64.b64decode(pdf_b64(10, filler=200_000))
    result = _pdf_session(tmp_path, raw, 10 * 4_568, paste_first=True, turns=3)
    assert result["ratios"][2] is not None and 2.9 <= result["ratios"][2] <= 3.0 * 1.005
    assert result["unflagged"] == []


# Reviewer A, PR #234, round 5: the append chain forms only for a true append ---------------------------------

def _chain_session(tmp_path, change: str, monkeypatch) -> list:
    """Three measured requests carrying the same screenshot, then a fourth changed by ``change``: none (a plain
    append), an edit to an earlier message, a rotation, a head change, a new screenshot, or a request
    forwarded unchanged after a failed transform. Returns what the fourth learned as a difference."""
    from contexto import rewrite as rw

    learned: list = []
    real_appended = rw.appended_usage
    monkeypatch.setattr(rw, "appended_usage", lambda *a: learned.append(real_appended(*a)) or learned[-1])
    eng = _engine(tmp_path, **({"bound": 40_000, "headroom_floor": 5_000, "min_turn_room": 1_000}
                               if change == "rotation" else {}))
    shot = image_block(data=png_b64(1200, 900, payload=2_000))
    msgs: list[dict] = [{"role": "user", "content": [{"type": "text", "text": "look " + "words " * 3_000}, shot]}]
    head = dict(HEAD)

    def send(messages):
        req = {**head, "messages": list(messages)}
        upstream, _, _ = eng(req, {}, EP)
        eng.observe_usage(EP, {"input_tokens": _real_usage(upstream), "output_tokens": 10}, req)

    send(msgs)
    for call in ("t0", "t1"):
        msgs += [{"role": "assistant", "content": [{"type": "tool_use", "id": call, "name": "Read", "input": {}}]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call,
                                               "content": "x = 1 " * 2_000}]}]
        send(msgs)
    learned.clear()
    if change == "edit":  # an earlier message, not the last one sent (2ef88a4 compared only that)
        msgs[2] = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t0",
                                                "content": "[Old tool result content cleared]"}]}
    if change == "head":
        head = {**HEAD, "tools": [{"name": "Extra", "input_schema": {"type": "object"}}]}
    if change == "media":
        other = image_block(data=png_b64(800, 600, payload=2_000))
        msgs[0] = {"role": "user", "content": [msgs[0]["content"][0], other]}
    if change == "rotation":
        msgs += [{"role": "assistant", "content": [{"type": "text", "text": "done " * 3_000}]},
                 {"role": "user", "content": [{"type": "text", "text": "next " + "words " * 6_000}]}]
    else:
        msgs += [{"role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "Read", "input": {}}]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2",
                                               "content": "y = 2 " * 2_000}]}]
    if change == "failed":
        state = next(iter(eng.states.values()))
        req = {**head, "messages": list(msgs)}
        _, forwarded, _ = rw.forward_original(req, msgs, state, "test", [])
        eng.states[next(iter(eng.states))] = forwarded
        eng.observe_usage(EP, {"input_tokens": _real_usage(req), "output_tokens": 10}, req)
        assert next(iter(eng.states.values())).last_measured is None  # a forwarded request starts no chain
        return learned
    send(msgs)
    return learned


def test_a_plain_append_with_the_same_media_learns_its_difference(tmp_path, monkeypatch):
    learned = _chain_session(tmp_path, "none", monkeypatch)
    assert len(learned) == 1 and learned[0] is not None and 2.9 <= learned[0][0] / learned[0][1] <= 3.1


@pytest.mark.parametrize("change", ["edit", "rotation", "head", "media", "failed"])
def test_the_chain_does_not_form_unless_the_request_only_appends(tmp_path, monkeypatch, change):
    """Round 5: only the last measured message was compared, so an edit to an earlier one (a window_changed
    resync keeps the epoch and window) chained, and its bytes went into the difference: 6 of 6 bound
    crossings unflagged against 2 of 6 for the control. Every sent message must be unchanged, in the same
    epoch, window, degraded state and head, with the same media."""
    assert all(result is None for result in _chain_session(tmp_path, change, monkeypatch))


@pytest.mark.parametrize("name", sorted(COUNTED_HIGH))
def test_files_counted_high_are_never_counted_low(name):
    """Round 7 (orchestrator's decision): a PDF is never counted low, so its pages are the largest count it
    offers. Pages an incremental save deleted, and page objects shown as text in a content stream, count
    too. The qdf listing counted 2 pages for 20 at 5971e84 (its stream skip ended at an endstream in text)."""
    count = pdf_page_count(_fixture(name))
    assert count.counted and count.pages >= COUNTED_HIGH[name]


def test_deleted_and_orphaned_pages_count_high_not_low():
    assert pdf_page_count(_fixture("pymupdf-incremental-deleted-40-to-10.pdf")).pages >= 10  # 40: deleted count
    assert pdf_page_count(base64.b64encode(_orphan_node_pdf()).decode()).pages >= 1  # 41: the orphan counts


def test_the_deleted_pages_file_pasted_first_is_not_advised_as_a_small_bound(tmp_path):
    """Its 40 counted pages (10 true) do not fit the window at the allowance; the shortfall is the PDF's
    allowance, so no larger --bound is advised (round 5, reviewer B)."""
    eng = _engine(tmp_path)
    doc = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                          "data": _fixture("pymupdf-incremental-deleted-40-to-10.pdf")}}
    req = {**HEAD, "messages": [{"role": "user", "content": [{"type": "text", "text": "read this"}, doc]}]}
    _, _, events = eng(req, {}, EP)
    assert all(e.get("media_allowance") for e in events if e["event"] == "bound_too_small")


# Rule 4's head with media in the conversation's first request (D49 amended with D48, round 5) ----------------

def _first_request(tmp_path, block: dict | None, cost: int, bound: int) -> tuple[int, int, int]:
    """One request carrying ``block`` (or, as the control, text of the same real cost), usage at 3.0 bytes a
    token plus the media's true cost. Returns (measured head, true head, W)."""
    eng = _engine(tmp_path, bound=bound)
    media = block if block is not None else {"type": "text", "text": "w" * (cost * 3)}
    req = {**HEAD, "messages": [{"role": "user", "content": [{"type": "text", "text": "look at this"}, media]}]}
    upstream, _, _ = eng(req, {}, EP)
    stripped, found = split_media(upstream["messages"])
    real = math.ceil(len(canonical_json({**upstream, "messages": stripped})) / 3.0) + cost * len(found)
    eng.observe_usage(EP, {"input_tokens": real, "output_tokens": 10}, req)
    state = next(iter(eng.states.values()))
    return state.head_tokens, math.ceil(len(canonical_json({**upstream, "messages": []})) / 3.0), state.window_budget


def _pdf_block(raw: bytes) -> dict:
    return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                           "data": base64.b64encode(raw).decode()}}


@pytest.mark.parametrize("case", ["screenshot", "dense-pdf", "deleted-pages", "orphan-node", "floor-runs-high"])
def test_media_in_the_first_request_measure_the_head(tmp_path, case):
    """Round 5 (reviewer B): rule 4 took the window's media at their floor, so a floor above the cost (the
    deleted-pages file and the orphan node counted 40 pages) took the head to 0 and W ran about 38k too
    large. The head now counts the window's text by its byte share of the request's text and its media at
    their floor, is never under the seed estimate of the request's own system prompt and tools, and with
    media never over its own bytes at 1.5 bytes a token (a dense PDF's floor is far under its cost)."""
    block, cost = {
        "screenshot": (image_block(), REAL_IMAGE_TOKENS),
        "dense-pdf": (_pdf_block(base64.b64decode(pdf_b64(10, filler=200_000))), 10 * 4_568),
        "deleted-pages": (_pdf_block(_fixture_bytes("pymupdf-incremental-deleted-40-to-10.pdf")), 10 * 1_583),
        "orphan-node": (_pdf_block(_orphan_node_pdf()), 1_583),
        "floor-runs-high": (_pdf_block(base64.b64decode(pdf_b64(20, filler=20_000))), 20 * 300),
    }[case]
    head, true_head, w = _first_request(tmp_path / "media", block, cost, 150_000)
    _, _, control_w = _first_request(tmp_path / "control", None, cost, 150_000)
    assert 0.85 * true_head <= head <= 2.05 * true_head  # 2ef88a4: 0 for deleted-pages and orphan-node
    assert w >= 0.85 * control_w


def test_a_100_page_pdf_in_the_first_request_at_800k(tmp_path):
    """Reviewer B, round 4: the floor (100,000) is far under the real cost (456,800), so the head took most of
    the PDF: W 226,625 against the control's 517,858, and three extra window_minimal."""
    block = _pdf_block(base64.b64decode(pdf_b64(100, filler=1_000_000)))
    head, true_head, w = _first_request(tmp_path / "media", block, 456_800, 800_000)
    _, _, control_w = _first_request(tmp_path / "control", None, 456_800, 800_000)
    assert true_head <= head <= 2.05 * true_head  # 2ef88a4: 376,643 for a true head of about 16,700
    assert w >= 0.95 * control_w and w <= 800_000 - 25_000 - true_head  # at least the control's, never over


# Round 6: objects shown inside content streams, the trailer's catalog, a bounded walk (reviewer A) ----------

def _listing_pdf(pages: int, *, length_marked: bool = True) -> bytes:
    """``pages`` pages whose uncompressed content streams show a minimal PDF's objects as text (a decoy
    catalog and a 1-page tree, and the word endstream), as reportlab writes with pageCompression=0."""
    shown = (b"BT (1 0 obj) Tj (<< /Type /Catalog /Pages 2 0 R >>) Tj (2 0 obj) Tj "
             b"(<< /Type /Pages /Kids [3 0 R] /Count 1 >>) Tj (3 0 obj) Tj (<< /Type /Page /Parent 2 0 R >>) Tj "
             b"(stream) Tj (endstream) Tj (endobj) Tj ET")
    kids = b" ".join(b"%d 0 R" % (3 + 2 * i) for i in range(pages))
    objs = [b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
            b"2 0 obj << /Type /Pages /Count %d /Kids [ %s ] >> endobj\n" % (pages, kids)]
    for i in range(pages):
        page, content = 3 + 2 * i, 4 + 2 * i
        objs.append(b"%d 0 obj << /Type /Page /Parent 2 0 R /Contents %d 0 R >> endobj\n" % (page, content))
        length = b"/Length %d" % len(shown) if length_marked else b"/Length 999 0 R"
        objs.append(b"%d 0 obj << %s >>\nstream\n" % (content, length) + shown + b"\nendstream\nendobj\n")
    return b"%PDF-1.4\n" + b"".join(objs) + b"trailer << /Size %d /Root 1 0 R >>\n%%%%EOF\n" % (4 + 2 * pages)


@pytest.mark.parametrize("length_marked", [True, False], ids=["direct-length", "indirect-length"])
@pytest.mark.parametrize("pages", [20, 100])
def test_objects_shown_inside_content_streams_never_lower_the_count(pages, length_marked):
    """Rounds 6 and 7: a page-tree walk read catalogs and nodes shown as text in content streams, so a
    tutorial PDF counted 1 page (3ac7984) and its indirect-/Length form 2 (5971e84); a 100-page one was
    estimated at 40,187 for a real 193,939 on a 150k bound, unflagged. The count is the largest the file
    offers, so text can only raise it."""
    assert pdf_page_count(base64.b64encode(_listing_pdf(pages, length_marked=length_marked)).decode()).pages >= pages


def test_a_decoy_catalog_does_not_lower_the_count():
    pages = 300
    kids = b" ".join(b"%d 0 R" % (i + 3) for i in range(pages))
    body = [b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
            b"2 0 obj << /Type /Pages /Count %d /Kids [ %s ] >> endobj\n" % (pages, kids)]
    body += [b"%d 0 obj << /Type /Page /Parent 2 0 R >> endobj\n" % (i + 3) for i in range(pages)]
    body += [b"900 0 obj << /Type /Catalog /Pages 901 0 R >> endobj\n",  # appended, unreferenced
             b"901 0 obj << /Type /Pages /Count 1 /Kids [ 3 0 R ] >> endobj\n"]
    raw = b"%PDF-1.4\n" + b"".join(body) + b"trailer << /Size 902 /Root 1 0 R >>\n%%EOF\n"
    assert pdf_page_count(base64.b64encode(raw).decode()).pages >= 300  # 3ac7984: (1, True, 1)


def test_a_crafted_wide_tree_is_bounded_in_memory_and_time():
    """Round 6: keeping every node's /Kids list took a 990-node, 1,000-reference tree to 41 MB a PDF."""
    objs = [b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"]
    for n in range(990):
        kids = b" ".join(b"%d 0 R" % (2 + (n + 1 + k) % 990) for k in range(1_000))
        objs.append(b"%d 0 obj << /Type /Pages /Count 600 /Kids [ %s ] >> endobj\n" % (n + 2, kids))
    raw = b"%PDF-1.4\n" + b"".join(objs) + b"trailer << /Root 1 0 R >>\n"
    result, seconds, peak = _timed_count(raw)
    assert result[0] == 600 and seconds < 0.5 and peak < 25 << 20


# Round 6 and 7: a PDF is priced at its allowance on every request, and the launcher's advice ---------------

def test_a_pdf_is_never_estimated_under_its_real_cost(tmp_path):
    """Round 7 (reviewer A): a learned per-PDF cost could come out far under the real cost (a cheap PDF
    learned at its floor was subtracted from the next one), and a re-Read near the bound went out
    unflagged: 146,786 estimated for 158,466 on 150k. Learning is removed; every request carrying the PDF
    is estimated at or above real, and every request over the bound is flagged before the send."""
    eng = _engine(tmp_path)
    slides = _pdf_block(base64.b64decode(pdf_b64(20, filler=6_000)))  # 20 pages costing 300 each
    paper = _pdf_block(base64.b64decode(pdf_b64(20, filler=16_000)))  # 20 pages costing 1,583 each
    costs = {slides["source"]["data"]: 20 * 300, paper["source"]["data"]: 20 * 1_583}
    rng = random.Random(12)
    msgs: list[dict] = [{"role": "user", "content": [{"type": "text", "text": "read the slides, then the paper"}]}]
    crossed = 0
    for step in range(200):
        req = {**HEAD, "messages": list(msgs)}
        upstream, _, events = eng(req, {}, EP)
        stripped, found = split_media(upstream["messages"])
        real = math.ceil(len(canonical_json({**upstream, "messages": stripped})) / 3.0) + sum(
            costs[b["source"]["data"]] for b in found)
        estimate = next(e for e in events if e["event"] == "request")["estimate"]
        if found:  # within text rounding once the PDF's request was measured (the estimate builds on it)
            assert estimate >= real * 0.995
        if real > 150_000:
            assert any(e["event"] == "over_bound" for e in events)
            crossed += 1
            if crossed > 2:
                break
        eng.observe_usage(EP, {"input_tokens": real, "output_tokens": 10}, req)
        content = ([slides] if step == 0 else [paper] if step in (1, 30) else
                   " ".join(rng.choices(["def", "x", "=", "return"], k=2_500)))
        use = {"type": "tool_use", "id": f"t{step}", "name": "Read", "input": {}}
        msgs += [{"role": "assistant", "content": [use]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{step}", "content": content}]}]
    assert crossed


def _hostile(pages: int, mode: str) -> bytes:
    """Reviewer A, round 7: a correct xref table, with the first content stream before the page objects and
    either a /Length that lies long, covering them, or no endstream at all."""
    content = b"BT /F1 10 Tf 72 700 Td (hi) Tj ET"
    first = 3 + pages
    tail = b"\nendstream" if mode == "long-length" else b""
    length = b"/Length 99999" if mode == "long-length" else b"/Length 99 0 R"
    objs = [b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
            b"%d 0 obj << %s >>\nstream\n%s%s\nendobj\n" % (first, length, content, tail),
            b"2 0 obj << /Type /Pages /Kids [ %s ] /Count %d >> endobj\n" % (
                b" ".join(b"%d 0 R" % (3 + i) for i in range(pages)), pages)]
    objs += [b"%d 0 obj << /Type /Page /Parent 2 0 R /Contents %d 0 R >> endobj\n" % (3 + i, first)
             for i in range(pages)]
    objs.append(b"99 0 obj %d endobj\n" % len(content))
    return b"%PDF-1.7\n" + b"".join(objs) + b"trailer << /Root 1 0 R >>\n%%EOF\n"


@pytest.mark.parametrize("mode", ["long-length", "missing-endstream"])
@pytest.mark.parametrize("pages", [20, 300])
def test_crafted_stream_lengths_never_lower_the_count(pages, mode):
    """Round 7 (reviewer A, low): a /Length that lies long, or a stream with no endstream, counted 1 to 12
    pages for 20 or 300. The whole file is scanned, so nothing hides the page objects."""
    assert pdf_page_count(base64.b64encode(_hostile(pages, mode)).decode()).pages >= min(pages, 600)


def test_the_qdf_listing_read_near_the_bound_is_flagged(tmp_path):
    """Round 7 (reviewer A): a qpdf --qdf file (every /Length indirect) counted 2 pages for 100, and reading
    it was estimated at 44,755 for a real 193,939 on 150k, unflagged."""
    eng = _engine(tmp_path)
    doc = _pdf_block((FIXTURES / "qdf-listing-20.pdf").read_bytes())
    msgs: list[dict] = [{"role": "user", "content": [{"type": "text", "text": "read the tutorial"}]},
                        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read",
                                                           "input": {}}]},
                        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [doc]}]}]
    upstream, _, events = eng({**HEAD, "messages": msgs}, {}, EP)
    stripped, _ = split_media(upstream["messages"])
    real = math.ceil(len(canonical_json({**upstream, "messages": stripped})) / 3.0) + 20 * 1_583
    assert next(e for e in events if e["event"] == "request")["estimate"] >= real


def _midpaste(tmp_path, pages: int, *, bound: int = 150_000, text_only: bool = False) -> list[dict]:
    """About 24k of history, then a PDF of ``pages`` typical pages (or text, ``text_only``) pasted with the
    next prompt. Returns that request's events."""
    eng = _engine(tmp_path, bound=bound)
    rng = random.Random(4)
    msgs: list[dict] = []
    doc = _pdf_block(base64.b64decode(pdf_b64(pages, filler=pages * 800)))
    for turn in range(3):
        extra = [] if turn < 2 else [{"type": "text", "text": "w" * 40_000} if text_only else doc]
        msgs.append({"role": "user", "content": [{"type": "text", "text": f"turn {turn} " + "talk " * 400}, *extra]})
        req = {**HEAD, "messages": list(msgs)}
        upstream, _, events = eng(req, {}, EP)
        if turn == 2:
            return events
        eng.observe_usage(EP, {"input_tokens": _real_usage(upstream), "output_tokens": 10}, req)
        for r in range(2):
            msgs += [{"role": "assistant", "content": [{"type": "tool_use", "id": f"t{turn}{r}", "name": "Read",
                                                        "input": {}}]},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{turn}{r}",
                                                   "content": " ".join(rng.choices(["def", "x", "="], k=3_000))}]}]
            req = {**HEAD, "messages": list(msgs)}
            upstream, _, _ = eng(req, {}, EP)
            eng.observe_usage(EP, {"input_tokens": _real_usage(upstream), "output_tokens": 10}, req)
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
    return []


def test_a_large_pdf_pasted_at_a_turn_start_is_not_advised_as_a_small_bound(tmp_path):
    """Round 6: 25 typical pages (real 57,606 on a 150k bound) logged bound_too_small, and the launcher told
    the user to restart with a larger --bound. The turn fits with the PDF at its floor, so the event says the
    shortfall is the media allowance; a bound that is really too small still says so."""
    events = _midpaste(tmp_path / "pdf", 25)
    (small,) = [e for e in events if e["event"] == "bound_too_small"]
    assert small.get("media_allowance") is True
    real_small = [e for e in _midpaste(tmp_path / "text", 25, bound=30_000, text_only=True)
                  if e["event"] == "bound_too_small"]
    assert real_small and not real_small[0].get("media_allowance")


def test_images_do_not_count_as_a_media_allowance(tmp_path):
    """Round 7 (reviewer A, low): an image's estimate is its price, not an allowance a response lowers, so a
    turn too large because of screenshots is a bound that is too small."""
    eng = _engine(tmp_path, bound=30_000, headroom_floor=5_000)
    shots = [image_block(data=png_b64(2560, 1600, payload=1_000, seed=i)) for i in range(4)]
    req = {**HEAD, "messages": [{"role": "user", "content": [{"type": "text", "text": "compare these"}, *shots]}]}
    _, _, events = eng(req, {}, EP)
    small = [e for e in events if e["event"] == "bound_too_small"]
    assert small and not any(e.get("media_allowance") for e in small)


def test_the_launcher_explains_a_media_allowance_and_still_warns_of_a_small_bound():
    import io

    from contexto.launcher import NoticeLog

    class Store:
        def log(self, event):
            pass

    err = io.StringIO()
    notices = NoticeLog(Store(), err)
    sizing = {"event": "bound_too_small", "bound": 150_000, "fixed_overhead": 45_242, "window_budget": 79_758}
    notices.log({"event": "request", "transform_events": [{**sizing, "media_allowance": True}]})
    assert "PDF is estimated generously" in err.getvalue() and "larger --bound" not in err.getvalue()
    assert "first response" not in err.getvalue()
    notices.log({"event": "request", "transform_events": [sizing]})  # a later, genuine one still shows
    assert "Restart with a larger --bound" in err.getvalue()
