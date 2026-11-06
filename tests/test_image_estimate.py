"""D48: images are priced by their pixels; images and documents are kept out of the bytes-per-token ratio.

Measured with count_tokens on claude-haiku-5-5 (QA, 2026-11-05): a 1440x900 PNG that Claude Code sends as
624,872 base64 characters costs 1,719 tokens (about w*h/750). On v0.5.8 the engine estimated it at about
178,000 tokens as a pasted image (3.5 bytes per token) and about 417,000 inside a tool_result (1.5 bytes
per token, how the Read tool returns screenshots). Documents (PDFs) are still estimated by their bytes, as
on v0.5.8, which errs high (a 3-page PDF really 4,748 is estimated at about 115,000 or more); pricing them
by page awaits a separate decision.
The session tests use a synthetic upstream that charges text at 3.0 bytes per token plus w*h/750 per image.
"""

from __future__ import annotations

import base64
import json
import math
import random
import struct
import zlib

import pytest

from contexto.canonical import canonical_bytes, canonical_json
from contexto.codex import RESPONSES
from contexto.rewrite import Config, RewriteEngine
from contexto.store import SessionStore
from contexto.store.memory import MemoryStore
from contexto.store.render import render_page
from contexto.tokens import (
    IMAGE_MAX_TOKENS,
    IMAGE_MIN_TOKENS,
    TokenEstimator,
    estimate_message_tokens,
    image_size,
    image_tokens,
    media_cost,
    message_bytes,
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


def pdf_b64(pages: int, *, filler: int = 300_000) -> str:
    """A PDF of ``pages`` page objects and a page tree, with ``filler`` bytes of binary data in a stream."""
    page = b"<< /Type /Page /Parent 2 0 R >>"
    objs = b"".join(b"%d 0 obj %s endobj\n" % (i + 3, page) for i in range(pages))
    kids = b" ".join(b"%d 0 R" % (i + 3) for i in range(pages))
    body = b"%%PDF-1.5\n2 0 obj << /Type /Pages /Count %d /Kids [ %s ] >> endobj\n" % (pages, kids) + objs
    data = random.Random(5).randbytes(filler)
    body += b"8 0 obj << /Length %d >>\nstream\n" % len(data) + data + b"\nendstream\nendobj\n"
    return base64.b64encode(body).decode()


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


def v058_estimate(messages: list[dict]) -> int:
    """v0.5.8's estimate (main before D48): every block by its canonical JSON bytes at the seeds."""
    text, tool = message_bytes(messages)
    return math.ceil(text / 3.5) + math.ceil(tool / 1.5)


def pdf_block(data: str) -> dict:
    return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}


def test_documents_are_estimated_by_their_bytes_as_before():
    """D48 prices images only; a PDF is estimated as on v0.5.8 (its bytes as text, which errs high), pasted
    or read by a tool, by base64, URL, or file id, until a separate decision prices it by page."""
    pasted = [{"role": "user", "content": [{"type": "text", "text": "read this"}, pdf_block(pdf_b64(3))]}]
    read = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                          "content": [pdf_block(pdf_b64(3)), image_block()]}]}]
    url = {"type": "document", "source": {"type": "url", "url": "https://example.com/a.pdf"}}
    file = {"type": "document", "source": {"type": "file", "file_id": "file_011"}}
    by_reference = [{"role": "user", "content": [url, file]}]
    for messages in (pasted, by_reference):
        assert estimate_message_tokens(messages) == v058_estimate(messages)
    assert estimate_message_tokens(pasted) > 100_000 > 3 * 4_568  # a 3-page PDF really costs about 4,748
    # In a Read result next to a screenshot: the PDF by its bytes at the tool-output seed, the image by pixels
    assert estimate_message_tokens(read) == v058_estimate(split_media(read, documents=False)[0]) + REAL_IMAGE_TOKENS
    measured = TokenEstimator()
    measured.observe(3_000, 1_000)
    assert measured.estimate_messages(pasted) == math.ceil(sum(message_bytes(pasted)) / 3.0)


def test_text_documents_are_measured_as_text():
    text = {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": "x" * 3_500}}
    assert 1_000 <= estimate_message_tokens([{"role": "user", "content": [text]}]) < 1_100


def test_split_media_copies_only_the_path_to_media():
    plain = {"type": "tool_result", "content": [{"type": "text", "text": "x"}]}
    assert split_media(plain) == (plain, []) and split_media(plain)[0] is plain
    image = image_block(data=png_b64(payload=10))
    mixed = {"type": "tool_result", "content": [{"type": "text", "text": "x"}, image]}
    stripped, found = split_media(mixed)
    assert found == [image] and stripped["content"] == [{"type": "text", "text": "x"}, None]
    assert mixed["content"][1] is image  # the input is not changed
    document = pdf_block(pdf_b64(1, filler=10))
    both = {"type": "tool_result", "content": [document, image]}
    assert split_media(both)[1] == [document, image]  # the ratio leaves both out
    assert split_media(both, documents=False) == ({"type": "tool_result", "content": [document, None]}, [image])


def test_codex_input_image_is_priced_by_pixels():
    url = "data:image/png;base64," + png_b64()
    items = [{"type": "message", "role": "user",
              "content": [{"type": "input_text", "text": "what is wrong?"}, {"type": "input_image", "image_url": url}]}]
    tokens = RESPONSES.estimate_items(TokenEstimator(), items)
    assert REAL_IMAGE_TOKENS <= tokens < REAL_IMAGE_TOKENS + 100  # v0.5.8: about 178,000
    pdf = {"type": "input_file", "filename": "a.pdf", "file_data": "data:application/pdf;base64," + pdf_b64(2)}
    items = [{"type": "message", "role": "user", "content": [pdf]}]
    assert RESPONSES.estimate_items(TokenEstimator(), items) == TokenEstimator().estimate(canonical_json(items))


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


def test_a_document_has_no_floor_for_the_ratio():
    """For the ratio a document is media with floor 0: its bytes are left out of the text, and no part of the
    real usage is taken off for it, so it can never raise the ratio (the image's floor stays)."""
    doc = pdf_block(pdf_b64(3))
    size = len(canonical_bytes(doc))
    assert media_cost([doc]) == (size, size, 0)
    image = image_block()
    assert media_cost([doc, image]).floor == media_cost([image]).floor > 0


# Unusual media --------------------------------------------------------------------------------------

def test_media_costs_never_raise():
    odd = [{"type": "image", "source": {"type": "base64", "data": "/9j/" + "A" * 10}},  # a JPEG cut short
           {"type": "image", "source": {"type": "base64", "data": 7}},
           {"type": "image", "source": "x"}, {"type": "input_image", "image_url": {"url": 5}},
           {"type": "document", "source": {"type": "base64", "data": "%%%"}}, {"type": "input_file"}]
    assert media_cost(odd).tokens > 0


def test_line_wrapped_base64_is_read():
    image = "\n".join(png_b64(payload=200)[i:i + 76] for i in range(0, 300, 76))
    assert estimate_message_tokens([{"role": "user", "content": [image_block(data=image)]}]) == REAL_IMAGE_TOKENS


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
    """The head is the first request's usage minus its window, with the window's media at their floor (0 for
    a document): its base64 is no text, so the window's share is its text's, and the head errs high."""
    eng = _engine(tmp_path)
    req = {**HEAD, "messages": [{"role": "user", "content": [{"type": "text", "text": "summarise"},
                                                             pdf_block(pdf_b64(20))]}]}
    upstream, _, _ = eng(req, {}, EP)
    stripped, _ = split_media(upstream)
    true_head = math.ceil(len(canonical_json({**stripped, "messages": []})) / 3.0)
    eng.observe_usage(EP, {"input_tokens": _real_usage(upstream) + 20 * 1_583, "output_tokens": 10}, req)
    state = next(iter(eng.states.values()))
    assert true_head <= state.head_tokens <= 2.05 * true_head  # main: 1,035 (the base64 took the share)


# A PDF in the ratio: floor 0, so it never raises it (reviewers A and B, rounds 2 to 4) ----------------------

def test_a_pdf_in_the_first_request_does_not_raise_the_ratio(tmp_path):
    """A 1-page PDF the upstream charges 1,583 for. At 23c42ea a crafted page claim took the ratio from 3.0 to
    5.73 and a later Read was estimated 11% low; on v0.5.8 its base64 took the ratio to about 36."""
    eng = _engine(tmp_path)
    msgs: list[dict] = [{"role": "user", "content": [{"type": "text", "text": "read this"},
                                                     pdf_block(pdf_b64(1, filler=40_000))]}]
    req = {**HEAD, "messages": list(msgs)}
    upstream, _, _ = eng(req, {}, EP)
    eng.observe_usage(EP, {"input_tokens": _real_usage(upstream) + 1_583, "output_tokens": 10}, req)
    ratio = eng.estimator.measured_bytes_per_token
    assert ratio is None or ratio <= 3.0
    rng = random.Random(7)
    text = " ".join(rng.choices(["def", "return", "self", "value", "x", "=", "(", ")"], k=30_000))
    msgs += [{"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "Read",
                                                "input": {"file_path": "big.py"}}]},
             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": text}]}]
    req = {**HEAD, "messages": list(msgs)}
    upstream, _, events = eng(req, {}, EP)
    estimate = next(e for e in events if e["event"] == "request")["estimate"]
    assert estimate >= _real_usage(upstream) + 1_583  # never under the real cost


def test_a_pdf_in_a_dense_session_does_not_raise_the_ratio(tmp_path):
    """Round 3: text at a true 2.0 bytes a token sits below the 3.5 ceiling, so a PDF whose floor ran over
    its cost lifted the ratio to 2.34 and the next request was estimated 4% low (d95e5df)."""
    eng = _engine(tmp_path)
    doc = pdf_block(pdf_b64(1, filler=8_000))

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
    assert ratio is None or ratio <= 2.0 * 1.01


# Reviewer B, PR #234, round 3: a dense PDF must not drag the ratio down ------------------------------------

def _dense_session(tmp_path, *, paste_first: bool, pages: int = 20) -> list[float | None]:
    """Text at 3.0 bytes a token, a PDF at 4,568 tokens a page (3,000 text plus a page image), read in a tool
    loop after a text turn or pasted in the first request, then 12 text turns with Reads. The ratio after
    each turn."""
    eng = _engine(tmp_path)
    doc = pdf_block(pdf_b64(pages, filler=pages * 20_000))
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
    pages) or 0.81 (100 pages) and took about 30 turns to recover, so the session kept less context. With a
    measured ratio the PDF is charged what keeps it."""
    ratios = _dense_session(tmp_path, paste_first=False)
    assert all(r is not None and 2.85 <= r <= 3.0 for r in ratios)


def test_a_dense_pdf_in_the_first_request_does_not_set_a_low_first_ratio(tmp_path):
    """Before a ratio exists the PDF's cost is unknown (its floor is 0, so its text would measure far under
    1.5 bytes a token) and the request is not learned from; d95e5df learned 0.82, and 1.65 ten turns later."""
    ratios = _dense_session(tmp_path, paste_first=True, pages=10)
    assert all(r is None or 2.7 <= r <= 3.0 for r in ratios) and ratios[-1] is not None


# Reviewer A, PR #234, round 4: PDFs of any cost, drift, and a PDF pasted first -------------------------------

def _pdf_session(tmp_path, raw: bytes, cost: int, *, paste_first: bool = False, dense_after: int | None = None,
                 turns: int = 7, read_words: int = 3_000, bound: int = 150_000) -> dict:
    """Text at 3.0 bytes a token (JSON tool output at 2.0 from turn ``dense_after``), the PDF costing ``cost``
    in all, read on turn 2 or pasted in the first request, then text turns with Reads and a tool loop that
    runs until a request crosses the bound; with no ``raw``, the same session without the PDF. Returns the
    ratios after each request and any request whose real usage crossed the bound without a pre-send
    over_bound."""
    eng = _engine(tmp_path, bound=bound)
    doc = pdf_block(base64.b64encode(raw).decode())
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


@pytest.mark.parametrize(("pages", "cost"), [(10, 10 * 300), (10, 10 * 1_583), (1, 1_583), (20, 20 * 4_568)],
                         ids=["cheap-pages", "typical", "one-page", "dense"])
def test_a_pdf_never_raises_the_ratio(tmp_path, pages, cost):
    """Round 4: once a ratio was measured, a floor above the PDF's real cost was booked as sparse text: the
    ratio rose to 3.75-5.06 (true 3.0) and requests crossed the bound unflagged. A PDF's floor is now 0, so
    whatever it costs the ratio never rises, and no request crosses the bound unflagged."""
    raw = base64.b64decode(pdf_b64(pages, filler=pages * 16_000))
    result = _pdf_session(tmp_path, raw, cost)
    assert max(r for r in result["ratios"] if r is not None) <= 3.0 * 1.005
    assert result["unflagged"] == []


def test_drift_to_denser_text_is_learned_with_a_pdf_in_view(tmp_path):
    """Round 4: with a 20-page PDF in the window, the floor-to-estimate slack absorbed the move to JSON tool
    output at 2.0 bytes a token, so the ratio held at 3.00 while the control fell. What a request appends
    to the last one, with the same media, is now learned as text."""
    raw = base64.b64decode(pdf_b64(20, filler=200_000))
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


@pytest.mark.parametrize("case", ["screenshot", "small-screenshot", "dense-pdf", "typical-pdf", "cheap-pdf"])
def test_media_in_the_first_request_measure_the_head(tmp_path, case):
    """Round 5 (reviewer B): rule 4 took the window's media at their floor, so a floor above the cost took
    the head to 0 and W ran about 38k too large. The head now counts the window's text by its byte share of
    the request's text and its media at their floor, is never under the seed estimate of the request's own
    system prompt and tools, and with media never over its own bytes at 1.5 bytes a token (a PDF's floor,
    0, is under any cost)."""
    block, cost = {
        "screenshot": (image_block(), REAL_IMAGE_TOKENS),
        "small-screenshot": (image_block(data=png_b64(400, 300, payload=2_000)), 160),
        "dense-pdf": (pdf_block(pdf_b64(10, filler=200_000)), 10 * 4_568),
        "typical-pdf": (pdf_block(pdf_b64(10, filler=100_000)), 10 * 1_583),
        "cheap-pdf": (pdf_block(pdf_b64(20, filler=20_000)), 20 * 300),
    }[case]
    head, true_head, w = _first_request(tmp_path / "media", block, cost, 150_000)
    _, _, control_w = _first_request(tmp_path / "control", None, cost, 150_000)
    assert 0.85 * true_head <= head <= 2.05 * true_head  # 2ef88a4: 0 when a floor ran over the cost
    assert w >= 0.85 * control_w


def test_a_100_page_pdf_in_the_first_request_at_800k(tmp_path):
    """Reviewer B, round 4: a floor far under the real cost (456,800) left the head most of the PDF: W 226,625
    against the control's 517,858, and three extra window_minimal. A PDF's floor is 0, so the limit at the
    head's own bytes is what holds it."""
    block = pdf_block(pdf_b64(100, filler=1_500_000))
    head, true_head, w = _first_request(tmp_path / "media", block, 456_800, 800_000)
    _, _, control_w = _first_request(tmp_path / "control", None, 456_800, 800_000)
    assert true_head <= head <= 2.05 * true_head  # 2ef88a4: 376,643 for a true head of about 16,700
    assert w >= 0.95 * control_w and w <= 800_000 - 25_000 - true_head  # at least the control's, never over


# Round 7: every request carrying a PDF is estimated at or above its real cost --------------------------------

def test_a_pdf_is_never_estimated_under_its_real_cost(tmp_path):
    """Round 7 (reviewer A): a learned per-PDF cost could come out far under the real cost, and a re-Read near
    the bound went out unflagged: 146,786 estimated for 158,466 on 150k. Every request carrying a PDF is
    estimated at or above real, and every request over the bound is flagged before the send."""
    eng = _engine(tmp_path)
    slides = pdf_block(pdf_b64(20, filler=60_000))  # 20 pages costing 300 each
    paper = pdf_block(pdf_b64(20, filler=200_000))  # 20 pages costing 1,583 each
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


def test_screenshots_too_large_for_the_bound_say_so(tmp_path):
    """An image's estimate is its price, so a turn too large because of screenshots is a bound that is too
    small, and bound_too_small says so as before."""
    eng = _engine(tmp_path, bound=30_000, headroom_floor=5_000)
    shots = [image_block(data=png_b64(2560, 1600, payload=1_000, seed=i)) for i in range(4)]
    req = {**HEAD, "messages": [{"role": "user", "content": [{"type": "text", "text": "compare these"}, *shots]}]}
    _, _, events = eng(req, {}, EP)
    small = [e for e in events if e["event"] == "bound_too_small"]
    assert small and all("media_allowance" not in e for e in small)
