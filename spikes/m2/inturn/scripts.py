"""The scripted conversations: what the model says and what the tools return, with no dialect in them.

A script is a pure function of its parameters: every text comes from SHA-256 of its position, so two
builds are byte-identical on every platform. Sizes are in the fake upstream's tokens
(``tokenizer.py``): a result "of 6,400 tokens" is text whose JSON is 6,400 x 2.2 bytes.

Each tool result starts and ends with a marker line of its own, so a run can tell whether a result is
still in the forwarded request or in a stored page (``harness.lost_originals``).
"""

from __future__ import annotations

import base64
import functools
import hashlib
import struct
import zlib
from dataclasses import dataclass, field

from inturn.tokenizer import TOOL_BYTES_PER_TOKEN

BOUND = 150_000
MAIN = "main"
SUBAGENT = "subagent"
AGENT_ID = "a36c784c53af45cc0"  # the id Claude Code gives the scripted subagent
WORDS = ("request", "handler", "session", "buffer", "config", "worker", "queue", "index", "token", "page",
         "window", "cache", "result", "parser", "stream", "record", "ledger", "bound", "retry", "store")


def digest(*parts: object) -> str:
    return hashlib.sha256(":".join(str(p) for p in parts).encode()).hexdigest()


def call_key(conversation: str, turn: int, step: int, n: int = 0) -> str:
    return digest("call", conversation, turn, step, n)


def claude_call_id(key: str) -> str:
    return "toolu_01" + key[:22]


def codex_call_id(key: str) -> str:
    return "call_" + key[:24]


def prose(seed: str, size: int) -> str:
    """About ``size`` bytes of plain sentences, for system prompts and tool descriptions."""
    out: list[str] = []
    total = i = 0
    while total < size:
        h = digest("prose", seed, i)
        words = [WORDS[int(h[j:j + 2], 16) % len(WORDS)] for j in range(0, 24, 2)]
        sentence = f"The {words[0]} {words[1]} keeps each {words[2]} {words[3]} until the {words[4]} " \
                   f"{words[5]} reports its {words[6]}, then the {words[7]} {words[8]} moves the {words[9]} " \
                   f"to the next {words[10]} {words[11]}."
        out.append(sentence)
        total += len(sentence) + 1
        i += 1
    return " ".join(out)[:size]


# Images ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Image:
    width: int
    height: int
    seed: str


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


@functools.cache
def _zeros_deflated(size: int) -> bytes:
    """A zlib stream holding ``size`` zero bytes: one fixed-Huffman block, a literal 0 and then
    back-references of length 258 at distance 1. Written by hand, so the bytes do not depend on which
    zlib the Python was built with."""
    bits: list[int] = [1, 1, 0]  # BFINAL, then BTYPE 01 (fixed Huffman), low bit first

    def code(value: int, width: int) -> None:  # a Huffman code: most significant bit first
        bits.extend((value >> (width - 1 - i)) & 1 for i in range(width))

    code(0b00110000, 8)  # literal 0
    left = size - 1
    while left >= 258:
        code(0b11000101, 8)  # length 258
        code(0, 5)  # distance 1
        left -= 258
    for _ in range(left):
        code(0b00110000, 8)
    code(0, 7)  # end of block
    bits.extend([0] * (-len(bits) % 8))
    body = bytes(sum(bit << i for i, bit in enumerate(bits[n:n + 8])) for n in range(0, len(bits), 8))
    return b"\x78\x01" + body + struct.pack(">I", zlib.adler32(bytes(size)))


@functools.cache
def png(image: Image) -> str:
    """A valid PNG of the image's size (1-bit greyscale, all black), base64. Its comment makes each one
    distinct. Only the header matters to pricing; the file is a few hundred bytes where a real
    screenshot is hundreds of kilobytes."""
    raw = image.height * (1 + (image.width + 7) // 8)  # a filter byte and the packed pixels, per row
    data = (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", image.width, image.height, 1, 0, 0, 0, 0))
            + _png_chunk(b"tEXt", b"Comment\x00" + image.seed.encode())
            + _png_chunk(b"IDAT", _zeros_deflated(raw))
            + _png_chunk(b"IEND", b""))
    return base64.b64encode(data).decode()


# Text -----------------------------------------------------------------------------------------------

def _line(tool: str, seed: str, i: int) -> str:
    h = digest("line", seed, i)
    if tool == "read":
        return f"{i + 1:>6}  {WORDS[i % len(WORDS)]}_{h[:8]} = load_{h[8:14]}({h[14:26]!r}, retries={int(h[26:28], 16)})"
    if tool == "grep":
        return f"src/{h[:6]}/{WORDS[i % len(WORDS)]}_{h[6:12]}.py:{int(h[12:15], 16)}:    return {h[15:27]}({h[27:39]})"
    return f"[{i:05d}] {WORDS[i % len(WORDS)]} {h[:6]} ok rc=0 bytes={int(h[6:12], 16)} sha={h[12:44]}"


def result_text(tool: str, seed: str, tokens: int) -> tuple[str, str, str]:
    """(text, first line, last line): tool output whose JSON string body is ``tokens`` x 2.2 bytes."""
    target = int(tokens * TOOL_BYTES_PER_TOKEN)
    head, tail = f"mark-{digest('head', seed)[:20]}-begin", f"mark-{digest('tail', seed)[:20]}-end"
    room = target - len(head) - len(tail) - 4  # each line break is two bytes in JSON
    lines: list[str] = []
    i = 0
    while room > 0:
        line = _line(tool, seed, i)[: max(0, room - 2)] if room - 2 < 90 else _line(tool, seed, i)
        lines.append(line)
        room -= len(line) + 2
        i += 1
    return "\n".join([head, *lines, tail]), head, tail


def thinking_text(seed: str, size: int) -> str:
    return (f"Checking {digest('think', seed)[:10]} against the last output. " + prose(seed, size))[:size]


# The script -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Call:
    key: str  # the call's id in each dialect comes from this (claude_call_id, codex_call_id)
    tool: str  # read, bash, grep, screenshot or agent
    arg: str  # the path, command, pattern or prompt
    result: str
    head: str = ""  # the result's first and last line, unique to it; "" for a receipt
    tail: str = ""
    images: tuple[Image, ...] = ()


@dataclass(frozen=True)
class Step:
    """One model response: thinking, optional text, and the tool calls with what each returns. A step
    with no call ends the turn."""

    thinking: str
    text: str = ""
    calls: tuple[Call, ...] = ()
    notice: str = ""  # Claude Code only: a system message the CLI adds after this step's results


@dataclass(frozen=True)
class Turn:
    prompt: str
    steps: tuple[Step, ...]
    images: tuple[Image, ...] = ()


@dataclass(frozen=True)
class Conversation:
    name: str
    turns: tuple[Turn, ...]
    agent: bool = False  # a Claude Code subagent: its own head, and the agent id header

    @property
    def steps(self) -> int:
        return sum(len(t.steps) for t in self.turns)

    @property
    def rounds(self) -> int:
        return sum(1 for t in self.turns for s in t.steps if s.calls)

    @property
    def calls(self) -> list[Call]:
        return [c for t in self.turns for s in t.steps for c in s.calls]


@dataclass(frozen=True)
class Scenario:
    name: str
    conversations: tuple[Conversation, ...]
    dialects: tuple[str, ...] = ("claude", "codex")
    order: tuple[str, ...] = ()  # which conversation sends each request; () for a single conversation
    bound: int = BOUND
    size: dict = field(default_factory=dict, compare=False)  # the parameters it was built with

    @property
    def schedule(self) -> tuple[str, ...]:
        return self.order or (self.conversations[0].name,) * self.conversations[0].steps


TOOL_CYCLE = ("read", "bash", "grep")


def _arg(tool: str, seed: str) -> str:
    h = digest("arg", seed)
    if tool == "read":
        return f"/work/app/src/{h[:6]}/{WORDS[int(h[6:8], 16) % len(WORDS)]}_{h[8:14]}.py"
    if tool == "grep":
        return f"{WORDS[int(h[:2], 16) % len(WORDS)]}_{h[2:8]}"
    if tool == "screenshot":
        return f"/tmp/shots/step-{h[:8]}.png"
    return f"pytest -q tests/test_{h[:8]}.py -k {WORDS[int(h[8:10], 16) % len(WORDS)]} 2>&1 | tail -400"


def tool_step(conversation: str, turn: int, step: int, tokens: int, *, thinking: int = 220,
              tool: str | None = None, images: tuple[Image, ...] = (), notice: str = "") -> Step:
    """A round: short thinking, one tool call, and its result of ``tokens`` tokens."""
    seed = f"{conversation}/{turn}/{step}"
    tool = tool or TOOL_CYCLE[step % len(TOOL_CYCLE)]
    text, head, tail = result_text("bash" if tool == "screenshot" else tool, seed, tokens)
    call = Call(call_key(conversation, turn, step), tool, _arg(tool, seed), text, head, tail, images)
    return Step(thinking_text(seed, thinking), calls=(call,), notice=notice)


def final_step(conversation: str, turn: int, step: int) -> Step:
    seed = f"{conversation}/{turn}/{step}/final"
    return Step(thinking_text(seed, 200), text="Done. " + prose(seed, 500))


def loop_turn(conversation: str, turn: int, sizes: list[int], *, thinking: int = 220) -> Turn:
    steps = [tool_step(conversation, turn, i, tokens, thinking=thinking) for i, tokens in enumerate(sizes)]
    prompt = f"Task {turn + 1}: " + prose(f"{conversation}/prompt/{turn}", 600)
    return Turn(prompt, (*steps, final_step(conversation, turn, len(sizes))))


def loop120(rounds: int = 120, result_tokens: int = 6_400) -> Scenario:
    """One prompt, then ``rounds`` tool rounds of ``result_tokens`` each: the 2026-11-12 audit's shape."""
    main = Conversation(MAIN, (loop_turn(MAIN, 0, [result_tokens] * rounds),))
    return Scenario("loop120", (main,), size={"rounds": rounds, "result_tokens": result_tokens})


def large(rounds: int = 12, big: tuple[int, ...] = (60_000, 120_000, 200_000), small_tokens: int = 800) -> Scenario:
    """A short loop where three results are each larger than the room a turn has."""
    at = {2 + 3 * i: tokens for i, tokens in enumerate(big)}  # rounds 3, 6 and 9
    main = Conversation(MAIN, (loop_turn(MAIN, 0, [at.get(i, small_tokens) for i in range(rounds)]),))
    return Scenario("large", (main,), size={"rounds": rounds, "big": list(big), "small_tokens": small_tokens})


def small(rounds: int = 1_500, result_tokens: int = 400) -> Scenario:
    """Many small results with short thinking: no single result matters."""
    main = Conversation(MAIN, (loop_turn(MAIN, 0, [result_tokens] * rounds, thinking=90),))
    return Scenario("small", (main,), size={"rounds": rounds, "result_tokens": result_tokens})


SCREENSHOTS = ((1_280, 800), (1_920, 1_080), (800, 600))
PASTED = (2_560, 1_600)


def images(rounds: int = 100, result_tokens: int = 2_500) -> Scenario:
    """A pasted image in the prompt; two rounds in three return a screenshot beside their text."""
    steps = []
    for i in range(rounds):
        shot = i % 3 != 2
        size = SCREENSHOTS[(i // 3 + i) % len(SCREENSHOTS)]
        pictures = (Image(*size, f"shot-{i}"),) if shot else ()
        steps.append(tool_step(MAIN, 0, i, result_tokens, tool="screenshot" if shot else None, images=pictures))
    turn = Turn("Compare the app with this mock-up. " + prose("images/prompt", 500),
                (*steps, final_step(MAIN, 0, rounds)), images=(Image(*PASTED, "pasted"),))
    return Scenario("images", (Conversation(MAIN, (turn,)),),
                    size={"rounds": rounds, "result_tokens": result_tokens,
                          "screenshots": sum(1 for s in steps if s.calls[0].images)})


def multiturn(loop_rounds: int = 100, result_tokens: int = 5_000, short_rounds: int = 2,
              short_tokens: int = 1_500) -> Scenario:
    """Three short turns, a long loop in turn 4, then two more short turns."""
    turns = [loop_turn(MAIN, t, [result_tokens] * loop_rounds if t == 3 else [short_tokens] * short_rounds)
             for t in range(6)]
    return Scenario("multiturn", (Conversation(MAIN, tuple(turns)),),
                    size={"loop_rounds": loop_rounds, "result_tokens": result_tokens,
                          "short_rounds": short_rounds, "short_tokens": short_tokens})


RECEIPT = ("Async agent launched successfully. (This tool result is internal metadata.)\n"
           f"agentId: {AGENT_ID} (internal ID - do not mention to user.)\nThe agent is working in the background. "
           "You will be notified automatically when it completes.")
TOOLS_USED = "<tools-used-before-summary>Read, Bash, Grep</tools-used-before-summary>"


def task_notice(call_id: str) -> str:
    """Claude Code's notice, during main's tool loop, that a background agent stopped (2.1.318)."""
    return (f"{TOOLS_USED}\n[SYSTEM NOTIFICATION - NOT USER INPUT]\nThis is an automated background-task event, "
            f"NOT a message from the user.\n\n<task-notification>\n<task-id>{AGENT_ID}</task-id>\n"
            f"<tool-use-id>{call_id}</tool-use-id>\n<output-file>/tmp/tasks/{AGENT_ID}.output</output-file>\n"
            "<status>completed</status>\n<summary>Agent \"audit the store\" completed</summary>\n</task-notification>")


def subagent(sub_rounds: int = 150, sub_tokens: int = 5_000, main_tokens: int = 1_200, before: int = 4,
             every: int = 3, after: int = 2) -> Scenario:
    """Main, in a tool loop, launches a background subagent and keeps working: one main request for
    every ``every`` of the subagent's. The subagent runs ``sub_rounds`` rounds in its own conversation.
    When it stops, main's next tool results are followed by Claude Code's task notice (the hand-back),
    and main finishes ``after`` rounds later."""
    prompt = "Audit the store module. " + prose("subagent/prompt", 700)
    launch = Call(call_key(MAIN, 0, before), "agent", prompt, RECEIPT)
    during = sub_rounds // every
    steps = [tool_step(MAIN, 0, i, main_tokens) for i in range(before)]
    steps.append(Step(thinking_text("main/launch", 220), calls=(launch,)))
    steps += [tool_step(MAIN, 0, before + 1 + i, main_tokens) for i in range(during)]
    told = before + 1 + during  # the first main round after the subagent stopped
    steps.append(tool_step(MAIN, 0, told, main_tokens, notice=task_notice(claude_call_id(launch.key))))
    steps += [tool_step(MAIN, 0, told + 1 + i, main_tokens) for i in range(after)]
    steps.append(final_step(MAIN, 0, len(steps)))
    main = Conversation(MAIN, (Turn("Review the storage layer. " + prose("main/prompt", 600), tuple(steps)),))
    sub_steps = [tool_step(SUBAGENT, 0, i, sub_tokens) for i in range(sub_rounds)]
    sub = Conversation(SUBAGENT, (Turn(prompt, (*sub_steps, final_step(SUBAGENT, 0, sub_rounds))),), agent=True)
    order = [MAIN] * (before + 1)  # the prompt, and each request that returns a round's results
    for i in range(during * every):
        order.append(SUBAGENT)
        if i % every == every - 1:
            order.append(MAIN)
    order += [SUBAGENT] * (sub.steps - during * every)
    order += [MAIN] * (main.steps - order.count(MAIN))
    return Scenario("subagent", (main, sub), dialects=("claude",), order=tuple(order),
                    size={"sub_rounds": sub_rounds, "sub_tokens": sub_tokens, "main_rounds": main.rounds,
                          "main_tokens": main_tokens})


BUILDERS = {"loop120": loop120, "large": large, "small": small, "subagent": subagent, "images": images,
            "multiturn": multiturn}
# The sizes the tests and the default command run. Only ``small`` is reduced: the engine's work for one
# request grows with the number of messages, so a loop's cost grows with the square of its rounds
# (about 4 s of CPU for 260 rounds, about 80 s for 1,500). ``--full`` runs FULL_SIZES.
TEST_SIZES: dict[str, dict] = {"small": {"rounds": 260}}
FULL_SIZES: dict[str, dict] = {"small": {"rounds": 1_500}}
# Small builds of every scenario, each still past the bound, for the tests that run a scenario twice
# or serialize every request.
QUICK_SIZES: dict[str, dict] = {
    "loop120": {"rounds": 26}, "large": {}, "small": {"rounds": 50, "result_tokens": 3_000},
    "subagent": {"sub_rounds": 30}, "images": {"rounds": 32, "result_tokens": 4_000},
    "multiturn": {"loop_rounds": 30},
}


def build(name: str, *, full: bool = False, quick: bool = False) -> Scenario:
    sizes = QUICK_SIZES if quick else FULL_SIZES if full else TEST_SIZES
    return BUILDERS[name](**sizes.get(name, {}))
