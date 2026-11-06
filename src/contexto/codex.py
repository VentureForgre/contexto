"""Codex adapter (M2 lane A, packet A5): the OpenAI Responses API dialect for the rewrite engine.

Shapes measured on codex-cli 0.154.0 with ``gpt-6-astra`` (zero-spend probes, spikes/m2/codex/, and
docs/research/M2-codex-adapter.md). A custom provider sends ``POST <base>/responses`` with the full
history in ``input`` on every request, ``store: false``, and no ``previous_response_id``. The first
items, before the user's first prompt, are the lead: an ``additional_tools`` developer item, the
base-instructions developer message, tagged developer context (permissions, collaboration mode, and
similar), and a user message carrying ``<environment_context>`` (and ``# AGENTS.md instructions``
when present). Later items are the conversation: user prompts, assistant messages, ``reasoning``
items with ``encrypted_content``, and tool calls with their outputs.

Mapping onto the contract's rules (``contexto.rewrite``):

- Turn start: a user ``message`` with authored content (a part that is not a context section).
- Lead: every item before the first turn start. It stays first in every upstream request and is
  never paged (the analogue of ``system`` and of the session-start message).
- System-like items: developer or system messages, the ``additional_tools`` item, and user messages
  made only of measured context sections (``CONTEXT_SECTION``). After the lead they are carried
  (other text, contract step 4): no per-turn Codex context shape is measured yet, and carrying is
  the contract's safe failure; non-text ones are unclassified.
- Thinking (rule 2): ``reasoning`` items of retained turns that existed when the epoch began are
  dropped. Dropping a reasoning item alone keeps call pairing valid; turns are paged whole, so a
  ``function_call`` and its output stay together.
- Preamble (rule 1): an ``input_text`` part prepended to the first retained turn start.
- Head (rule 6): top-level ``instructions`` and ``tools`` plus the lead items.
- Usage: ``response.usage.input_tokens`` already includes cached tokens, so it is the input total.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from contexto import paths
from contexto.canonical import canonical_bytes, loads_bounded
from contexto.rewrite import CARRIED_TEXT, SESSION_START, UNCLASSIFIED, Dialect
from contexto.tokens import TokenEstimator
from contexto.upstream import claude_key_helper, untrusted_note

MODEL_OUTPUT_TYPES = frozenset({"reasoning", "function_call", "custom_tool_call", "local_shell_call",
                                "web_search_call", "image_generation_call", "tool_search_call"})
TEXT_PART_TYPES = frozenset({"input_text", "output_text", "text"})
# Context sections Codex injects as messages, anchored to the exact openings measured on codex-cli
# 0.154.0 (spikes/m2/codex/probe.sh: skills_instructions, permissions instructions, collaboration_mode,
# multi_agent_role, multi_agent_mode, environment_context) and the AGENTS.md header from the 0.154.0
# source. A user prompt that merely starts with another tag (such as <div>) stays a turn start.
# Rechecked on 0.163.0 for D38, with zero spend against loopback stubs: spikes/m2/cliversion/check.sh
# (one `codex exec`) and spikes/m2/codex/probe.sh (five auth and provider scenarios) showed the same
# six openings in every scenario, the environment_context message last in the lead; the AGENTS.md
# header was not exercised. Rechecked on 0.166.0 for D56 the same way: the same six openings; only the
# built-in provider's requests (never Contexto's custom provider) add a role-less configuration_update item.
CONTEXT_SECTIONS_CODEX_VERSION = "0.166.0"
CONTEXT_SECTION = re.compile(
    r"\s*(?:<(?:skills_instructions|permissions instructions|collaboration_mode|multi_agent_role|"
    r"multi_agent_mode|environment_context)>|# AGENTS\.md instructions for )")


def _parts(item: dict) -> list:
    content = item.get("content")
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    return content if isinstance(content, list) else []


def _damaged_block() -> dict:
    """The block ``retained_blocks`` gives for a part or output of the wrong type (a damaged page file):
    ``render_page`` shows it as ``[malformed text omitted]`` and search skips it, as for a Messages API
    text block whose text is not a string, so a partly damaged output does not read as complete."""
    return {"type": "text", "text": None}


def _is_damaged_part(part: Any) -> bool:
    """A part no Responses API request holds: not an object, a non-string ``type``, or a text part whose
    text is not a string. Other part types (images, files) are left out on purpose."""
    if not isinstance(part, dict) or not isinstance(part.get("type"), str):
        return True
    return part["type"] in TEXT_PART_TYPES and not isinstance(part.get("text"), str)


def _is_message(item: dict) -> bool:
    return item.get("type", "message") == "message"


def _is_text_part(part: Any) -> bool:
    """A text part with string text. Any other value is skipped, so a damaged page file renders and is
    searched without raising (an unhashable ``type`` cannot be looked up in a set)."""
    return (isinstance(part, dict) and isinstance(part.get("type"), str) and part["type"] in TEXT_PART_TYPES
            and isinstance(part.get("text"), str))


def is_context_part(part: Any) -> bool:
    return _is_text_part(part) and bool(CONTEXT_SECTION.match(part["text"]))


class ResponsesDialect(Dialect):
    name = "responses"
    field = "input"
    media_floor = False  # D48: OpenAI prices images by tiles (85 tokens at low detail), under Claude's floor

    def is_context_block(self, block: Any) -> bool:
        return False  # Codex sends context as separate messages, not inside the prompt message

    def is_turn_start(self, item: dict) -> bool:
        if not _is_message(item) or item.get("role") != "user":
            return False
        parts = _parts(item)
        return any(isinstance(p, dict) and not is_context_part(p) for p in parts)

    def is_assistant(self, item: dict) -> bool:
        return item.get("type") in MODEL_OUTPUT_TYPES or (_is_message(item) and item.get("role") == "assistant")

    def is_system_like(self, item: dict) -> bool:
        if item.get("type") == "additional_tools":
            return True
        if not _is_message(item):
            return False
        if item.get("role") in ("developer", "system"):
            return True
        parts = _parts(item)
        return item.get("role") == "user" and bool(parts) and all(is_context_part(p) for p in parts)

    def result_ids(self, item: dict) -> list[str]:
        """D40: the call id a ``*_output`` item (``function_call_output``, ``custom_tool_call_output``,
        ``local_shell_call_output``, ...) returns a result for."""
        if not str(item.get("type", "")).endswith("_output"):
            return []
        call_id = item.get("call_id", item.get("id"))
        return [call_id] if isinstance(call_id, str) else []

    def retained_blocks(self, item: dict) -> list[tuple[str, dict]]:
        """A14: Responses items as the Messages API blocks ``render_page`` renders, so an in-view line
        reads like a paged one: message text parts (not context sections), calls as ``tool_use``, call
        outputs as ``tool_result``. Reasoning, images, and other items are not searchable."""
        kind = item.get("type", "message")
        if not isinstance(kind, str):  # a damaged page file: shown as damaged, not left out unseen
            return [(item.get("role", "unknown"), _damaged_block())]
        if kind == "message":
            role = item.get("role", "unknown")
            content = item.get("content")
            if content is not None and not isinstance(content, (str, list)):
                return [(role, _damaged_block())]
            return [(role, _damaged_block() if _is_damaged_part(p) else {"type": "text", "text": p["text"]})
                    for p in _parts(item) if _is_damaged_part(p) or (_is_text_part(p) and not is_context_part(p))]
        if kind in ("function_call", "custom_tool_call"):
            raw = item.get("arguments") if kind == "function_call" else item.get("input")
            try:
                args = loads_bounded(raw) if isinstance(raw, str) else raw
            except ValueError:
                args = raw  # not JSON, or nested deeper than MAX_JSON_DEPTH: rendered as the string it is
            return [("assistant", {"type": "tool_use", "name": item.get("name", ""),
                                   "input": {} if args is None else args})]
        if kind in ("function_call_output", "custom_tool_call_output"):
            out = item.get("output")
            if isinstance(out, str):
                content = out
            elif isinstance(out, list):
                content = [_damaged_block() if _is_damaged_part(p) else {"type": "text", "text": p["text"]}
                           for p in out if _is_damaged_part(p) or _is_text_part(p)]
            else:  # None: no output; anything else is damaged
                content = [] if out is None else [_damaged_block()]
            return [("user", {"type": "tool_result", "content": content})]
        return []

    def classify_system(self, items: list[dict], i: int) -> str:
        if i < self.lead_count(items):
            return SESSION_START
        if item_is_text_only(items[i]):
            return CARRIED_TEXT
        return UNCLASSIFIED

    def normalise(self, item: dict) -> dict:
        return item

    def head_fingerprint(self, request: dict) -> str:
        items = request.get("input") or []
        head = {"instructions": request.get("instructions"), "tools": request.get("tools"),
                "lead": items[: self.lead_count(items)]}
        return hashlib.sha256(canonical_bytes(head)).hexdigest()

    def head_estimate_source(self, request: dict) -> dict:
        return {"instructions": request.get("instructions"), "tools": request.get("tools")}

    def strip_thinking(self, item: dict) -> dict | None:
        return None if item.get("type") == "reasoning" else item

    def merge_first(self, first: dict, blocks: list, text: str) -> dict:
        own = first.get("content")
        own_parts = [{"type": "input_text", "text": own}] if isinstance(own, str) else list(own or [])
        head = [*blocks] + ([{"type": "input_text", "text": text}] if text else [])
        return {**first, "content": head + own_parts}

    def countable(self, item: dict) -> bool:
        return True

    def estimate_items(self, estimator: TokenEstimator, items: list[dict]) -> int:
        return estimator.estimate_value(items) if items else 0

    def lead_count(self, items: list[dict]) -> int:
        for i, item in enumerate(items):
            if self.is_turn_start(item):
                return i
        return len(items)

    def usage_tokens(self, usage: dict) -> int:
        return usage.get("input_tokens") or 0  # cached_tokens is a subset of input_tokens

    def judges_session_start(self, request: dict) -> bool:
        return True  # Codex's main requests carry no top-level tools in code mode; every request has the lead

    def session_start_seen(self, items: list[dict]) -> bool | None:
        """D38: True when the lead holds a user message made only of context sections (on 0.154.0 and
        0.163.0, ``<environment_context>``). False when the lead holds Codex's developer items but no
        such message: a Codex that changed its section tags makes that message a turn start, and it
        would be paged. None when the lead has no developer or system message (not Codex's shape)."""
        lead = items[: self.lead_count(items)]
        if any(_is_message(i) and i.get("role") == "user" and self.is_system_like(i) for i in lead):
            return True
        return False if any(_is_message(i) and i.get("role") in ("developer", "system") for i in lead) else None

    @property
    def session_start_version(self) -> str:
        return CONTEXT_SECTIONS_CODEX_VERSION


def item_is_text_only(item: dict) -> bool:
    if item.get("type") == "additional_tools" or not _is_message(item):
        return False
    return all(isinstance(p, dict) and p.get("type") in TEXT_PART_TYPES for p in _parts(item))


RESPONSES = ResponsesDialect()


# Launch planning (intake decision D22) -----------------------------------------------------------
#
# API-key mode, the default and the only mode run live: a private CODEX_HOME (directory 0700, config
# 0600, removed on exit) whose custom provider points at the proxy and gets its bearer token from
# ``auth.command`` running the existing key helper through ``/bin/sh -c``. The config names the helper
# command only, never a key. ChatGPT mode (``--codex-auth chatgpt``): the user's own CODEX_HOME with
# ``-c`` overrides only and ``requires_openai_auth``: Codex keeps its own login and the proxy passes it
# through. Under D35 logins belong in CLIProxyAPI (reached in API-key mode) or the vendor's own CLI,
# never in Contexto, and users follow their providers' terms. Both keep Codex on HTTP (custom
# providers have no WebSocket transport) and turn request compression off, because the proxy cannot
# transform compressed bodies. Some Codex calls (rate limits, usage, a remote MCP
# connection) go to chatgpt.com directly and bypass the proxy by design.

API_KEY_MODE = "apikey"
CHATGPT_MODE = "chatgpt"
KEY_HELPER_ENV = "CONTEXTO_CODEX_KEY_HELPER"
UPSTREAM_ENV = "CONTEXTO_CODEX_UPSTREAM"
CHATGPT_UPSTREAM = "https://chatgpt.com/backend-api/codex"
ANTHROPIC_DOMAIN = "anthropic.com"
PROVIDER_ID = "contexto"
CHATGPT_MODE_HELP = ("ChatGPT login: route the user's own Codex ChatGPT credentials through the local proxy, "
                     "tested only with synthetic credentials. Logins belong in CLIProxyAPI or the vendor's own "
                     "CLI, never in Contexto, and you must follow your provider's terms "
                     "(https://github.com/VentureForgre/contexto/blob/main/docs/cliproxyapi.md).")


CODEX_BIN = "codex"
# Codex runs ``auth.command`` itself, with ``args``, without a shell (codex-rs ModelProviderAuthInfo,
# 0.166.0). Claude Code runs ``apiKeyHelper`` through a shell, and Contexto reuses that helper, so the
# helper string goes to /bin/sh -c as one argument: ``~``, ``$VAR``, pipes and quoting work as they do
# for Claude Code (D54).
KEY_HELPER_SHELL = "/bin/sh"
# Codex's interactive TUI starts a shared background app-server for each CODEX_HOME and leaves it
# running after the TUI exits, with an updater that downloads new Codex releases into the home
# (measured on 0.166.0: two processes and 517 MB per home, 936 MB after one self-update).
# Each Contexto session has its own CODEX_HOME, so each session would leave a pair behind; interactive
# launches pass --no-daemon instead (D54). The flag is a top-level option since 0.156.0
# (codex-rs/tui/src/cli.rs: absent at rust-v0.155.0, present at rust-v0.156.0); 0.166.0 accepts it
# before every subcommand. exec starts no server (measured on 0.166.0), and neither it nor the other
# subcommands that do not open the TUI get the flag (``agents`` refuses it: it needs the shared
# server); resume and fork open the TUI and do. When ``codex --version`` cannot be read, ``codex
# --help`` decides: the flag is passed if it lists it, else the user is warned once.
NO_DAEMON_FLAG = "--no-daemon"
NO_DAEMON_SINCE = (0, 156, 0)
HELP_TIMEOUT_S = 3.0
INTERACTIVE_SUBCOMMANDS = ("resume", "fork")
# ``codex agents`` browses the shared server's sessions and refuses --no-daemon ("requires a shared
# server"). Measured on 0.166.0 under Contexto: it installs and starts the server pair in the session's
# CODEX_HOME and leaves both running; ``codex app-server daemon stop`` then stops the server but not its
# pid-update-loop. The server's work would also outlive the proxy it is configured to use. So it is
# refused, unless ``--remote`` points it at a server elsewhere.
SHARED_SERVER_SUBCOMMANDS = ("agents",)
# Top-level options that take a value, so the subcommand is found after them (codex --help, 0.166.0).
# ``-i``/``--image`` takes every following word up to the next option: ``codex -i a.png exec`` opens the
# TUI with two images, while ``--image=a.png exec`` runs exec.
VALUE_OPTIONS = ("-c", "--config", "--enable", "--disable", "--remote", "--remote-auth-token-env", "-m", "--model",
                 "--local-provider", "-p", "--profile", "-s", "--sandbox", "-C", "--cd", "--add-dir", "-a",
                 "--ask-for-approval")
MULTI_VALUE_OPTIONS = ("-i", "--image")
SUBCOMMANDS = ("agents", "exec", "e", "review", "login", "logout", "mcp", "plugin", "app-server", "remote-control",
               "app", "completion", "update", "doctor", "sandbox", "debug", "apply", "a", "resume", "queue",
               "archive", "delete", "migrate-rollouts", "migrate-nfs-db", "unarchive", "fork", "cloud",
               "exec-server", "features", "help")  # codex --help, 0.166.0
# A kept CODEX_HOME is kept for its rollouts. Codex 0.166.0's daemon also copies its release into
# packages/ (417 MB; a launch with --no-daemon makes none), which cleanup removes. The plugin
# marketplace clone in .tmp/ (96 MB) stays, so a resumed session does not download it again; plugins
# are left to the user (README).
DISPOSABLE_HOME_DIRS = ("packages",)
# Window budget at start (contract rule 4, launcher.start_warning): Codex's fixed overhead measured
# 13,322 to 18,403 tokens (docs/research/M2-codex-step1-live.md, M2-codex-acceptance-150k.md) and
# 15,694 on 0.166.0 (QA, 2026-11-05); the largest, rounded up.
ASSUMED_OVERHEAD = 19_000


class CodexLaunchError(RuntimeError):
    pass


@dataclass
class CodexLaunch:
    argv: list[str]
    env: dict[str, str]
    upstream: str
    codex_home: Path | None = None  # CODEX_HOME written for this launch (API-key mode)
    notes: list[str] = field(default_factory=list)
    keep_home: bool = False  # True when CODEX_HOME lives in the Contexto session dir (rollouts kept)

    def cleanup(self) -> None:
        if self.codex_home is None:
            return
        if self.keep_home:
            (self.codex_home / "config.toml").unlink(missing_ok=True)
            for name in DISPOSABLE_HOME_DIRS:
                shutil.rmtree(self.codex_home / name, ignore_errors=True)
        else:
            shutil.rmtree(self.codex_home, ignore_errors=True)
        self.codex_home = None


def claude_api_key_helper(cwd: Path, home: Path, env: dict[str, str] | None = None) -> str | None:
    """The ``apiKeyHelper`` Claude Code would use, from the same settings files and order as the
    upstream resolution (``$CLAUDE_CONFIG_DIR/settings.json`` for the user's when ``env`` sets it),
    a project file's only when Claude Code has recorded the folder as trusted (D52)."""
    return claude_key_helper(cwd, home, env).value


def _toml(value: object) -> str:
    """TOML literal for strings, booleans, and lists of strings (JSON escapes are valid TOML). TOML also
    forbids a raw DEL, which JSON leaves as is, and has no escape for a lone surrogate."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    text = str(value)
    if any(0xD800 <= ord(c) <= 0xDFFF for c in text):
        raise CodexLaunchError("a value for Codex's config holds a lone surrogate, which TOML cannot hold")
    return json.dumps(text, ensure_ascii=False).replace("\x7f", "\\u007f")


# Exec mode runs with approval policy "never", which refuses every MCP call that needs approval, so
# recall would never work there (measured in the A6 smoke, docs/research/M2-codex-smoke.md). The
# contexto tools only touch the user's local data (they read the session store and log their calls, pin
# and close append to its ledger, contexto_state hashes the files ledger records cite), so they are
# pre-approved; D44 approves the same tools for Claude Code.
# codex-cli 0.154.0 accepts auto, prompt, writes, or approve.
MCP_APPROVAL_MODE = "approve"


# Codex looks an MCP server's command up on PATH and, when it cannot start the server, runs the turn
# without its tools and reports nothing (measured on codex-cli 0.163.0: a bare "contexto" not on PATH
# left code mode's ALL_TOOLS with no contexto tool). The server is started as the Claude path starts it
# (launcher.mcp_payload): this interpreter by absolute path, "-P -m contexto" (-P keeps the
# project directory Codex starts it in off sys.path, so a project's own contexto package cannot
# shadow the installed one).
def _mcp_server(session_id: str) -> tuple[str, list[str]]:
    return sys.executable, ["-P", "-m", "contexto", "mcp", "--session", session_id]


def api_key_config(proxy_url: str, helper: str, session_id: str, contexto_home: Path,
                   env: dict[str, str] | None = None) -> str:
    mcp_command, mcp_args = _mcp_server(session_id)
    if not helper.strip():
        raise CodexLaunchError("the key helper command is empty")
    lines = [
        f"model_provider = {_toml(PROVIDER_ID)}",
        "",
        f"[model_providers.{PROVIDER_ID}]",
        f"name = {_toml('Contexto')}",
        f"base_url = {_toml(proxy_url)}",
        f"wire_api = {_toml('responses')}",
        f"supports_websockets = {_toml(False)}",
        "",
        f"[model_providers.{PROVIDER_ID}.auth]",
        f"command = {_toml(KEY_HELPER_SHELL)}",
        f"args = {_toml(['-c', helper])}",
        "",
        "[features]",
        f"enable_request_compression = {_toml(False)}",
        "",
        f"[mcp_servers.{PROVIDER_ID}]",
        f"command = {_toml(mcp_command)}",
        f"args = {_toml(mcp_args)}",
        f"default_tools_approval_mode = {_toml(MCP_APPROVAL_MODE)}",
        "",
        # Codex starts MCP servers with a restricted environment, so the server is told the home
        # explicitly, always, as the Claude path does (launcher.mcp_payload): otherwise it opens
        # ~/.contexto, a different store than the proxy's (packet A18). CONTEXTO_SEARCH_CACHE_MB too, when set (D39).
        f"[mcp_servers.{PROVIDER_ID}.env]",
        *(f"{k} = {_toml(v)}" for k, v in paths.mcp_env(contexto_home, env or {}).items()),
        "",
    ]
    return "\n".join(lines)


def chatgpt_overrides(proxy_url: str, session_id: str, contexto_home: Path,
                      env: dict[str, str] | None = None) -> list[str]:
    mcp_command, mcp_args = _mcp_server(session_id)
    provider = (f"{{name={_toml('Contexto')}, base_url={_toml(proxy_url)}, wire_api={_toml('responses')}, "
                f"requires_openai_auth=true, supports_websockets=false}}")
    return ["-c", f"model_providers.{PROVIDER_ID}={provider}", "-c", f"model_provider={PROVIDER_ID}",
            "-c", "features.enable_request_compression=false",
            "-c", f"mcp_servers.{PROVIDER_ID}.command={_toml(mcp_command)}",
            "-c", f"mcp_servers.{PROVIDER_ID}.args={_toml(mcp_args)}",
            "-c", f"mcp_servers.{PROVIDER_ID}.default_tools_approval_mode={_toml(MCP_APPROVAL_MODE)}",
            *(arg for k, v in paths.mcp_env(contexto_home, env or {}).items()
              for arg in ("-c", f"mcp_servers.{PROVIDER_ID}.env.{k}={_toml(v)}"))]


def resolve_codex_upstream(auth_mode: str, explicit: str | None, env: dict[str, str],
                           claude_upstream: str | None) -> str:
    """The proxy's upstream for Codex, which carries the version path: ``--codex-upstream``, else
    ``CONTEXTO_CODEX_UPSTREAM``, else the ChatGPT backend in ChatGPT mode, else the upstream Claude Code
    would use (for example a CLIProxyAPI root, D35) plus ``/v1`` in API-key mode."""
    return codex_upstream_with_source(auth_mode, explicit, env, claude_upstream)[0]


def codex_upstream_with_source(auth_mode: str, explicit: str | None, env: dict[str, str],
                               claude_upstream: str | None, claude_source: str | None = None) -> tuple[str, str]:
    """``resolve_codex_upstream`` plus where the URL came from, for the session line and log (D35):
    ``codex-flag``, ``codex-env``, ``chatgpt-default``, or ``claude-<source>+/v1`` (the Claude
    upstream's own source label, such as ``claude-settings:local+/v1``). Never the URL itself."""
    if auth_mode not in (API_KEY_MODE, CHATGPT_MODE):
        raise CodexLaunchError(f"unknown Codex auth mode: {auth_mode}")
    if explicit:
        return explicit, "codex-flag"
    if env.get(UPSTREAM_ENV):
        return env[UPSTREAM_ENV], "codex-env"
    if auth_mode == CHATGPT_MODE:
        return CHATGPT_UPSTREAM, "chatgpt-default"
    if claude_upstream:
        label = f"claude-{claude_source or 'upstream'}+/v1"
        host = _anthropic_host(claude_upstream)
        if host:
            # Claude Code's own API (its default, or a base URL set to it) does not serve the Responses
            # API: Codex's /responses would 404 there, after sending the user's key to the wrong vendor.
            raise CodexLaunchError(
                f"no upstream for Codex: Claude Code's upstream ({claude_source or 'upstream'}) is {host}, "
                f"which does not serve Codex. With an OpenAI API key pass --codex-upstream "
                f"https://api.openai.com/v1; with CLIProxyAPI pass its URL plus /v1 (or set {UPSTREAM_ENV}).")
        return claude_upstream.rstrip("/") + "/v1", label
    raise CodexLaunchError(f"no upstream for Codex: pass --codex-upstream or set {UPSTREAM_ENV}")


def _anthropic_host(url: str) -> str | None:
    """The host name of ``url`` when it is Anthropic's (``anthropic.com`` or a subdomain, compared after
    IDNA normalization as ``proxy.is_vendor_host`` does), else None."""
    try:
        host = urlsplit(url).hostname
        host = host.encode("idna").decode("ascii").lower().rstrip(".") if host else ""
    except (ValueError, UnicodeError):  # not a URL the proxy accepts either; it reports that
        return None
    return host if host == ANTHROPIC_DOMAIN or host.endswith("." + ANTHROPIC_DOMAIN) else None


def takes_no_daemon(codex_args: list[str], codex_version: str | None, help_lists_flag: bool = False) -> bool:
    """Whether to pass ``--no-daemon``: only for an interactive launch where the user chose no server
    (``--no-daemon``, ``--remote``), on a Codex that takes it: by version when it was read (0.156.0 or
    later), else when ``codex --help`` lists the flag (``help_lists_flag``)."""
    if not _may_take_no_daemon(codex_args):
        return False
    version = _version_tuple(codex_version)
    return version >= NO_DAEMON_SINCE if version else help_lists_flag


def _may_take_no_daemon(codex_args: list[str]) -> bool:
    if any(a == NO_DAEMON_FLAG or a == "--remote" or a.startswith("--remote=") for a in codex_args):
        return False
    subcommand = _subcommand(codex_args)
    return subcommand is None or subcommand in INTERACTIVE_SUBCOMMANDS


def _subcommand(codex_args: list[str]) -> str | None:
    """The subcommand ``codex`` would run: the first word after the leading options, if it names one."""
    i = 0
    while i < len(codex_args):
        arg = codex_args[i]
        if arg == "--":  # the rest is the prompt
            return None
        if arg in MULTI_VALUE_OPTIONS:
            i += 1
            while i < len(codex_args) and not codex_args[i].startswith("-"):
                i += 1
        elif arg in VALUE_OPTIONS:
            i += 2
        elif arg.startswith("-"):
            i += 1
        else:
            return arg if arg in SUBCOMMANDS else None
    return None


def needs_shared_server(codex_args: list[str]) -> str | None:
    """The subcommand when it runs only with Codex's local shared server (``SHARED_SERVER_SUBCOMMANDS``)
    and ``--remote`` does not send it elsewhere, else None."""
    if any(a == "--remote" or a.startswith("--remote=") for a in codex_args):
        return None
    subcommand = _subcommand(codex_args)
    return subcommand if subcommand in SHARED_SERVER_SUBCOMMANDS else None


def help_lists_no_daemon(codex: str) -> bool:
    """Whether ``codex --help`` lists ``--no-daemon`` (bounded by ``HELP_TIMEOUT_S``, in its own process
    group, stdin closed; False when it cannot be run)."""
    try:
        out = subprocess.run([codex, "--help"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=HELP_TIMEOUT_S, start_new_session=True)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return re.search(rb"(?m)^\s*--no-daemon\b", out.stdout) is not None


def _version_tuple(version: str | None) -> tuple[int, ...]:
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version or "")
    return tuple(int(p) for p in match.groups()) if match else ()


def plan_codex(codex_args: list[str], proxy_url: str, session_id: str, *, auth_mode: str = API_KEY_MODE,
               key_helper: str | None = None, upstream: str | None = None,
               codex_bin: str = "codex", env: dict[str, str] | None = None, cwd: Path | None = None,
               home: Path | None = None, claude_upstream: str | None = None,
               codex_home_dir: Path | None = None, contexto_home: Path | None = None,
               no_daemon: bool = False) -> CodexLaunch:
    """Plan a ``codex`` launch behind the proxy at ``proxy_url`` (no ``/v1``: the upstream carries the
    version path). The caller starts the proxy with ``launch.upstream``, runs ``launch.argv`` with
    ``launch.env``, and calls ``launch.cleanup()`` on exit. ``no_daemon`` (``takes_no_daemon``) puts
    ``--no-daemon`` first. Nothing here reads or writes a credential."""
    base_env = dict(os.environ if env is None else env)
    contexto_home = Path(contexto_home) if contexto_home is not None else paths.contexto_home(base_env)
    cwd = cwd or Path.cwd()
    home = home or Path.home()
    target = resolve_codex_upstream(auth_mode, upstream, base_env, claude_upstream)
    flags = [NO_DAEMON_FLAG] if no_daemon else []
    if auth_mode == CHATGPT_MODE:
        # --no-daemon here too, so the TUI does not attach to a server already running for the user's
        # own CODEX_HOME, started without the proxy's -c overrides (whether it would honor them is not
        # measured).
        argv = [codex_bin, *flags, *chatgpt_overrides(proxy_url, session_id, contexto_home, base_env),
                *codex_args]
        return CodexLaunch(argv=argv, env=base_env, upstream=target, notes=[CHATGPT_MODE_HELP])
    helper = key_helper or base_env.get(KEY_HELPER_ENV)
    notes: list[str] = []
    if not helper:
        # A project's apiKeyHelper only from a folder Claude Code trusts: Codex runs it unasked (D52).
        found = claude_key_helper(cwd, home, base_env)
        helper = found.value
        if found.ignored:
            notes.append(untrusted_note("apiKeyHelper", found.ignored,
                                        f"Pass --codex-key-helper (or set {KEY_HELPER_ENV})", base_env))
    if not helper:
        raise CodexLaunchError(f"no key helper: {notes[0]}" if notes else
                               f"no key helper: pass --codex-key-helper, set {KEY_HELPER_ENV}, "
                               "or configure apiKeyHelper in Claude Code settings")
    if codex_home_dir is not None:
        # Kept under the Contexto session directory so Codex's rollouts stay resumable; only the
        # config (which names the helper command) is removed on exit.
        codex_home = Path(codex_home_dir)
        codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        keep = True
    else:
        codex_home = Path(tempfile.mkdtemp(prefix="contexto-codex-"))
        keep = False
    config = codex_home / "config.toml"
    try:
        codex_home.chmod(0o700)
        if config.exists():
            config.unlink()  # a stale config from an interrupted run
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(api_key_config(proxy_url, helper, session_id, contexto_home, base_env))
    except BaseException:
        if keep:
            config.unlink(missing_ok=True)
        else:
            shutil.rmtree(codex_home, ignore_errors=True)
        raise
    return CodexLaunch(argv=[codex_bin, *flags, *codex_args], env={**base_env, "CODEX_HOME": str(codex_home)},
                       upstream=target, codex_home=codex_home, notes=notes, keep_home=keep)


# The run command's codex branch (agreed with builder-1: commands/run.py calls add_arguments and
# dispatches 'contexto run codex' to run_codex; the proxy and child-process helpers are his, in
# contexto.launcher) ------------------------------------------------------------------------------

def add_arguments(parser) -> None:
    group = parser.add_argument_group("codex")
    group.add_argument("--codex-auth", choices=[API_KEY_MODE, CHATGPT_MODE], default=API_KEY_MODE,
                       help=f"how Codex authenticates (default {API_KEY_MODE}: a private CODEX_HOME in the session "
                            f"directory whose provider runs the key helper). {CHATGPT_MODE}: {CHATGPT_MODE_HELP}")
    group.add_argument("--codex-key-helper", default=None,
                       help=f"command that prints the API key, run with {KEY_HELPER_SHELL} -c as Claude Code runs "
                            f"apiKeyHelper (default ${KEY_HELPER_ENV}, else Claude Code's apiKeyHelper)")
    group.add_argument("--codex-upstream", default=None,
                       help=f"upstream for Codex requests, with its version path, such as https://api.openai.com/v1 "
                            f"(default ${UPSTREAM_ENV}, else the ChatGPT backend in chatgpt mode, else Claude Code's "
                            "upstream plus /v1 unless that is Anthropic's API)")


def run_codex(args, *, launcher=None, stderr=None) -> int:
    """Run ``codex`` behind a Contexto proxy; returns the exit code (codex's, or a launcher error).
    What can refuse the launch up front (the options, a ``codex`` that is missing or cannot run, the
    upstream) is checked before the session and the proxy exist; a launch that fails after that, before
    Codex starts (no key helper, for example from an untrusted folder, D52), removes the session
    directory it created, so a failed launch leaves no empty session (D54); a resumed ``--session`` is
    never removed."""
    import uuid

    from contexto import cliversions
    from contexto import upstream as upstream_module

    if launcher is None:
        from contexto import launcher  # builder-1's module; imported lazily
    stderr = stderr or sys.stderr
    codex_args = list(args.cli_args)
    created: Path | None = None  # the session directory this launch made
    try:
        options = launcher.options_from_args(args)
        launcher.check_options(options)
        codex = shutil.which(CODEX_BIN)
        if codex is None:  # named as `run claude` names it: missing, not executable, a directory, a broken link
            raise launcher.LaunchError(launcher.cli_not_found(CODEX_BIN), launcher.EXIT_NOT_FOUND)
        # `codex --version` both shows that Codex can start at all (an executable file with a missing #!
        # interpreter passes `which`) and gives the version --no-daemon is gated on.
        try:
            version, version_error = cliversions.installed_version(codex)
        except KeyboardInterrupt:  # its process group was killed first
            print("contexto: interrupted", file=stderr, flush=True)
            return launcher.EXIT_INTERRUPTED
        except Exception as exc:  # unreadable, as when it prints no version
            version, version_error = None, type(exc).__name__
        if _start_failure(version_error):
            raise launcher.LaunchError(f"'{CODEX_BIN}' at {codex} cannot be run ({version_error}); check its #! "
                                       "line or reinstall Codex", launcher.EXIT_NOT_FOUND)
        shared = needs_shared_server(codex_args)
        if shared:
            raise CodexLaunchError(
                f"'codex {shared}' needs Codex's shared background server, which would keep running for its "
                f"CODEX_HOME after Codex exits (Codex refuses {NO_DAEMON_FLAG} with it), so Contexto does not "
                f"run it; run 'codex {shared}' without Contexto, or pass --remote.")
        # Codex ignores .claude/, so a project's base URL only from a folder Claude Code trusts (D52).
        claude = upstream_module.resolve_upstream(args.upstream, trusted_only=True)
        ignored = (untrusted_note("env.ANTHROPIC_BASE_URL", claude.ignored, "Pass --codex-upstream", os.environ)
                   if claude.ignored else None)
        try:
            target, source = codex_upstream_with_source(args.codex_auth, args.codex_upstream, dict(os.environ),
                                                        claude.url, claude.source)
        except CodexLaunchError:
            # A refusal of the derived upstream (Anthropic's, or none): first say why Claude Code's
            # upstream fell back, when an untrusted project's base URL was skipped.
            if ignored:
                print(f"contexto: {ignored}", file=stderr)
            raise
        if ignored and source.startswith("claude-"):
            print(f"contexto: {ignored}", file=stderr)
        no_daemon = False
        if _may_take_no_daemon(codex_args):
            if version_error is None and not _version_tuple(version):
                version_error = "unparsed"
            try:
                listed = help_lists_no_daemon(codex) if version_error else False
            except KeyboardInterrupt:
                print("contexto: interrupted", file=stderr, flush=True)
                return launcher.EXIT_INTERRUPTED
            no_daemon = takes_no_daemon(codex_args, version, listed)
        home = paths.contexto_home()
        session_id = args.session if args.session is not None else uuid.uuid4().hex  # '' is invalid (D57)
        try:
            existed = os.path.lexists(paths.session_dir(home, session_id))
            store = launcher.open_session(home, session_id, Path.cwd())
        except (OSError, ValueError) as exc:  # as `contexto run claude` reports it: 2 for the id, 1 for the store
            raise launcher.LaunchError(f"cannot open session {session_id[:40]!r}: {exc}",
                                       launcher.EXIT_USAGE if isinstance(exc, ValueError) else launcher.EXIT_PROXY
                                       ) from exc
        created = None if existed else paths.session_dir(home, session_id)
        config = launcher.engine_config(options)
        proxy, engine_name = launcher.start_proxy(store, target, source, config, args.observe_thinking,
                                                  stderr=stderr)
    except (CodexLaunchError, launcher.LaunchError) as exc:
        print(f"contexto: {exc}", file=stderr)
        _remove_session(created)
        return getattr(exc, "code", 2)
    plan = None
    started = False
    code = launcher.EXIT_PROXY
    try:
        plan = plan_codex(codex_args, proxy.url, session_id, auth_mode=args.codex_auth,
                          key_helper=args.codex_key_helper, upstream=target, codex_bin=codex,
                          codex_home_dir=paths.session_dir(home, session_id) / "codex-home",
                          contexto_home=home, no_daemon=no_daemon)
        for note in plan.notes:
            print(f"contexto: {note}", file=stderr)
        store.log({"event": "launch", "cli": "codex", "session": session_id, "config": config,
                   "engine": engine_name, "upstream_source": source, "codex_auth": args.codex_auth,
                   "observe_thinking": args.observe_thinking, "user_arg_count": len(codex_args),
                   "affinity_header": getattr(proxy, "affinity_header", None) is not None,
                   "no_daemon": no_daemon,
                   **({"codex_version_error": version_error} if version_error else {})})
        print(f"contexto: session {session_id} (bound {options.bound:,}, upstream {source}, codex auth "
              f"{args.codex_auth}, engine {engine_name})", file=stderr, flush=True)
        if version_error and not no_daemon and _may_take_no_daemon(codex_args):
            # Without the flag Codex may leave its shared server running for this session's CODEX_HOME.
            print(f"contexto: warning: cannot read the Codex version ({version_error}) and 'codex --help' does not "
                  f"list {NO_DAEMON_FLAG}, so it is not passed; Codex may leave a background app-server running "
                  "after it exits.", file=stderr, flush=True)
        if not launcher.check_cli_version("codex", codex, store,
                                          enabled=not getattr(args, "no_version_check", False), stderr=stderr):
            code = launcher.EXIT_INTERRUPTED  # Ctrl+C during the version check; codex never started
            return code
        warning = launcher.start_warning(options.bound, options.headroom_floor, ASSUMED_OVERHEAD)
        if warning:
            print(f"contexto: {warning}", file=stderr, flush=True)
            store.log({"event": "window_budget_warning", "bound": options.bound, "assumed_overhead": ASSUMED_OVERHEAD})
        started = True
        # Codex's shell commands (`contexto state`, `contexto search`) then name this session (D57).
        code = launcher.run_child(plan.argv, {**plan.env, "CONTEXTO_SESSION": session_id}, stderr=stderr)
        return code
    except CodexLaunchError as exc:
        print(f"contexto: {exc}", file=stderr)
        code = 2
        return code
    finally:
        if plan is not None:
            plan.cleanup()
        proxy.stop()
        launcher.report_missing_tools(proxy)  # Codex reports no MCP server that fails to start
        store.log({"event": "exit", "code": code})
        if not started:
            _remove_session(created)


def _start_failure(error: str | None) -> bool:
    """Whether ``installed_version``'s error means ``codex`` could not be started at all: the name of an
    ``OSError`` from starting it (FileNotFoundError for a missing #! interpreter, PermissionError,
    OSError for an exec format error), not a timeout, an exit code, or unparsed output."""
    cls = getattr(builtins, error or "", None)
    return isinstance(cls, type) and issubclass(cls, OSError) and not issubclass(cls, TimeoutError)


def _remove_session(created: Path | None) -> None:
    if created is not None:
        shutil.rmtree(created, ignore_errors=True)
