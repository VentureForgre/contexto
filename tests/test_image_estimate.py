"""D48: images and documents are priced by pixels and pages, and kept out of the bytes-per-token ratio.

Measured with count_tokens on claude-haiku-5-5 (QA, 2026-11-05): a 1440x900 PNG that Claude Code sends as
624,872 base64 characters costs 1,719 tokens (about w*h/750). On v0.5.8 the engine estimated it at about
178,000 tokens as a pasted image (3.5 bytes per token) and about 417,000 inside a tool_result (1.5 bytes
per token, how the Read tool returns screenshots); a 3-page PDF really 4,748 was estimated at 498,000.
The session tests use a synthetic upstream that charges text at 3.0 bytes per token plus w*h/750 per image.
"""

from __future__ import annotations

import base64
import math
import random
import struct
import zlib

import pytest

from contexto.canonical import canonical_json
from contexto.codex import RESPONSES
from contexto.rewrite import Config, RewriteEngine
from contexto.store import SessionStore
from contexto.store.render import render_page
from contexto.tokens import (
    IMAGE_MAX_TOKENS,
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


def image_block(seed: int = 1, data: str | None = None, media_type: str = "image/png") -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                        "data": data if data is not None else png_b64(seed=seed)}}


def pdf_b64(pages: int, *, object_stream: bool = False, filler: int = 300_000) -> str:
    objs = b"".join(b"%d 0 obj << /Type /Page /Parent 2 0 R >> endobj\n" % (i + 3) for i in range(pages))
    if object_stream:
        packed = zlib.compress(objs)
        objs = b"9 0 obj << /Type /ObjStm /N %d /Filter /FlateDecode /Length %d >>\nstream\n" % (
            pages, len(packed)) + packed + b"\nendstream endobj\n"
    body = b"%PDF-1.5\n2 0 obj << /Type /Pages /Kids [] >> endobj\n" + objs
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
    assert image_tokens(10, 10) == 1


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
    pdf = base64.b64encode(b"%PDF-1.4\n" + random.Random(3).randbytes(300_000)).decode()
    messages = [{"role": "user", "content": [{"type": "document", "source": {
        "type": "base64", "media_type": "application/pdf", "data": pdf}}]}]
    # A 300 KB PDF whose page tree cannot be read counts one page per 50 KB: 7 pages, not 115,000 tokens.
    assert estimate_message_tokens(messages) == 7 * PDF_PAGE_TOKENS < 40_000


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
    assert pdf_page_count(guessed) == (7, False) and pdf_page_count(pdf_b64(3)) == (3, True)
    doc = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": guessed}}
    assert media_cost([doc]) == (0, 7 * PDF_PAGE_TOKENS, PDF_FLOOR_PAGE_TOKENS)
    counted = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64(3)}}
    assert media_cost([counted]).floor == 3 * PDF_FLOOR_PAGE_TOKENS
