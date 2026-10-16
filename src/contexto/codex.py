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
  made only of context sections. After the lead they are carried (other text, contract step 4),
  since no Codex hook-output signature is documented yet; non-text ones are unclassified.
- Thinking (rule 2): ``reasoning`` items of retained turns that existed when the epoch began are
  dropped. Dropping a reasoning item alone keeps call pairing valid; turns are paged whole, so a
  ``function_call`` and its output stay together.
- Preamble (rule 1): an ``input_text`` part prepended to the first retained turn start.
- Head (rule 6): top-level ``instructions`` and ``tools`` plus the lead items.
- Usage: ``response.usage.input_tokens`` already includes cached tokens, so it is the input total.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contexto.canonical import canonical_bytes, canonical_json
from contexto.rewrite import CARRIED_TEXT, SESSION_START, UNCLASSIFIED, Dialect
from contexto.tokens import TokenEstimator
from contexto.upstream import claude_settings_order

MODEL_OUTPUT_TYPES = frozenset({"reasoning", "function_call", "custom_tool_call", "local_shell_call",
                                "web_search_call", "image_generation_call", "tool_search_call"})
TEXT_PART_TYPES = frozenset({"input_text", "output_text", "text"})
# Context sections Codex injects as messages (measured tags, and AGENTS.md per the 0.154.0 source).
CONTEXT_SECTION = re.compile(r"\s*(<[A-Za-z_][\w -]*>|# AGENTS\.md instructions)")


def _parts(item: dict) -> list:
    content = item.get("content")
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    return content if isinstance(content, list) else []


def _is_message(item: dict) -> bool:
    return item.get("type", "message") == "message"


def is_context_part(part: Any) -> bool:
    return (isinstance(part, dict) and part.get("type") in TEXT_PART_TYPES
            and isinstance(part.get("text"), str) and bool(CONTEXT_SECTION.match(part["text"])))


class ResponsesDialect(Dialect):
    name = "responses"
    field = "input"

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
        return estimator.estimate(canonical_json(items)) if items else 0

    def lead_count(self, items: list[dict]) -> int:
        for i, item in enumerate(items):
            if self.is_turn_start(item):
                return i
        return len(items)

    def usage_tokens(self, usage: dict) -> int:
        return usage.get("input_tokens") or 0  # cached_tokens is a subset of input_tokens


def item_is_text_only(item: dict) -> bool:
    if item.get("type") == "additional_tools" or not _is_message(item):
        return False
    return all(isinstance(p, dict) and p.get("type") in TEXT_PART_TYPES for p in _parts(item))


RESPONSES = ResponsesDialect()


# Launch planning (intake decision D22) -----------------------------------------------------------
#
# API-key mode, the default and the only mode run live: a temporary CODEX_HOME (directory 0700,
# config 0600, removed on exit) whose custom provider points at the proxy and gets its bearer token
# from ``auth.command`` running the existing key helper. The config names the helper command only,
# never a key. ChatGPT mode (``--codex-auth chatgpt``): the user's own CODEX_HOME with ``-c`` overrides
# only and ``requires_openai_auth``; its end-user terms question is open as O4. Both keep Codex on
# HTTP (custom providers have no WebSocket transport) and turn request compression off, because the
# proxy cannot transform compressed bodies. Some Codex calls (rate limits, usage, a remote MCP
# connection) go to chatgpt.com directly and bypass the proxy by design.

API_KEY_MODE = "apikey"
CHATGPT_MODE = "chatgpt"
KEY_HELPER_ENV = "CONTEXTO_CODEX_KEY_HELPER"
UPSTREAM_ENV = "CONTEXTO_CODEX_UPSTREAM"
CHATGPT_UPSTREAM = "https://chatgpt.com/backend-api/codex"
PROVIDER_ID = "contexto"
CHATGPT_MODE_HELP = ("ChatGPT login: route the user's own Codex ChatGPT credentials through the local proxy. "
                     "Whether end users may do this under OpenAI's terms is an open owner question (O4).")


class CodexLaunchError(RuntimeError):
    pass


@dataclass
class CodexLaunch:
    argv: list[str]
    env: dict[str, str]
    upstream: str
    codex_home: Path | None = None  # temporary CODEX_HOME to remove with cleanup()
    notes: list[str] = field(default_factory=list)

    def cleanup(self) -> None:
        if self.codex_home is not None:
            shutil.rmtree(self.codex_home, ignore_errors=True)
            self.codex_home = None


def claude_api_key_helper(cwd: Path, home: Path) -> str | None:
    """The ``apiKeyHelper`` Claude Code would use, from the same settings files and order as the
    upstream resolution (the main checkout's local settings first in a linked worktree)."""
    for _kind, path in claude_settings_order(cwd, home):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        value = data.get("apiKeyHelper") if isinstance(data, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _toml(value: object) -> str:
    """TOML literal for strings, booleans, and lists of strings (JSON escapes are valid TOML)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def _mcp_args(session_id: str) -> list[str]:
    return ["mcp", "--session", session_id]


def api_key_config(proxy_url: str, helper: str, session_id: str, contexto_bin: str = "contexto") -> str:
    parts = shlex.split(helper)
    if not parts:
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
        f"command = {_toml(parts[0])}",
        f"args = {_toml(parts[1:])}",
        "",
        "[features]",
        f"enable_request_compression = {_toml(False)}",
        "",
        f"[mcp_servers.{PROVIDER_ID}]",
        f"command = {_toml(contexto_bin)}",
        f"args = {_toml(_mcp_args(session_id))}",
        "",
    ]
    return "\n".join(lines)


def chatgpt_overrides(proxy_url: str, session_id: str, contexto_bin: str = "contexto") -> list[str]:
    provider = (f"{{name={_toml('Contexto')}, base_url={_toml(proxy_url)}, wire_api={_toml('responses')}, "
                f"requires_openai_auth=true, supports_websockets=false}}")
    return ["-c", f"model_providers.{PROVIDER_ID}={provider}", "-c", f"model_provider={PROVIDER_ID}",
            "-c", "features.enable_request_compression=false",
            "-c", f"mcp_servers.{PROVIDER_ID}.command={_toml(contexto_bin)}",
            "-c", f"mcp_servers.{PROVIDER_ID}.args={_toml(_mcp_args(session_id))}"]


def resolve_codex_upstream(auth_mode: str, explicit: str | None, env: dict[str, str],
                           claude_upstream: str | None) -> str:
    """The proxy's upstream for Codex, which carries the version path: ``--codex-upstream``, else
    ``CONTEXTO_CODEX_UPSTREAM``, else the ChatGPT backend in ChatGPT mode, else the upstream Claude Code
    would use (the owner's proxy) plus ``/v1`` in API-key mode."""
    if auth_mode not in (API_KEY_MODE, CHATGPT_MODE):
        raise CodexLaunchError(f"unknown Codex auth mode: {auth_mode}")
    target = explicit or env.get(UPSTREAM_ENV)
    if not target and auth_mode == CHATGPT_MODE:
        target = CHATGPT_UPSTREAM
    if not target and claude_upstream:
        target = claude_upstream.rstrip("/") + "/v1"
    if not target:
        raise CodexLaunchError(f"no upstream for Codex: pass --codex-upstream or set {UPSTREAM_ENV}")
    return target


def plan_codex(codex_args: list[str], proxy_url: str, session_id: str, *, auth_mode: str = API_KEY_MODE,
               key_helper: str | None = None, upstream: str | None = None, contexto_bin: str = "contexto",
               codex_bin: str = "codex", env: dict[str, str] | None = None, cwd: Path | None = None,
               home: Path | None = None, claude_upstream: str | None = None) -> CodexLaunch:
    """Plan a ``codex`` launch behind the proxy at ``proxy_url`` (no ``/v1``: the upstream carries the
    version path). The caller starts the proxy with ``launch.upstream``, runs ``launch.argv`` with
    ``launch.env``, and calls ``launch.cleanup()`` on exit. Nothing here reads or writes a credential."""
    base_env = dict(os.environ if env is None else env)
    cwd = cwd or Path.cwd()
    home = home or Path.home()
    target = resolve_codex_upstream(auth_mode, upstream, base_env, claude_upstream)
    if auth_mode == CHATGPT_MODE:
        return CodexLaunch(argv=[codex_bin, *chatgpt_overrides(proxy_url, session_id, contexto_bin), *codex_args],
                           env=base_env, upstream=target, notes=[CHATGPT_MODE_HELP])
    helper = key_helper or base_env.get(KEY_HELPER_ENV) or claude_api_key_helper(cwd, home)
    if not helper:
        raise CodexLaunchError(f"no key helper: pass --codex-key-helper, set {KEY_HELPER_ENV}, "
                               "or configure apiKeyHelper in Claude Code settings")
    codex_home = Path(tempfile.mkdtemp(prefix="contexto-codex-"))
    try:
        codex_home.chmod(0o700)
        config = codex_home / "config.toml"
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(api_key_config(proxy_url, helper, session_id, contexto_bin))
    except BaseException:
        shutil.rmtree(codex_home, ignore_errors=True)
        raise
    return CodexLaunch(argv=[codex_bin, *codex_args], env={**base_env, "CODEX_HOME": str(codex_home)},
                       upstream=target, codex_home=codex_home)


# The run command's codex branch (agreed with builder-1: commands/run.py calls add_arguments and
# dispatches 'contexto run codex' to run_codex; the proxy and child-process helpers are his, in
# contexto.launcher) ------------------------------------------------------------------------------

def add_arguments(parser) -> None:
    group = parser.add_argument_group("codex")
    group.add_argument("--codex-auth", choices=[API_KEY_MODE, CHATGPT_MODE], default=API_KEY_MODE,
                       help=f"how Codex authenticates (default {API_KEY_MODE}: a temporary CODEX_HOME whose provider "
                            f"runs the key helper). {CHATGPT_MODE}: {CHATGPT_MODE_HELP}")
    group.add_argument("--codex-key-helper", default=None,
                       help=f"command that prints the API key (default ${KEY_HELPER_ENV}, else Claude Code's apiKeyHelper)")
    group.add_argument("--codex-upstream", default=None,
                       help=f"upstream for Codex requests, with its version path (default ${UPSTREAM_ENV}, else "
                            "the ChatGPT backend in chatgpt mode, else Claude Code's upstream plus /v1)")


def run_codex(args, *, launcher=None, stderr=None) -> int:
    """Run ``codex`` behind a Contexto proxy; returns the exit code (codex's, or a launcher error)."""
    import sys
    import uuid

    from contexto import paths, upstream as upstream_module

    if launcher is None:
        from contexto import launcher  # builder-1's module; imported lazily
    stderr = stderr or sys.stderr
    try:
        launcher.check_bound(args.bound, args.allow_large_bound)
        options = launcher.options_from_args(args)
        claude = upstream_module.resolve_upstream(args.upstream)
        target = resolve_codex_upstream(args.codex_auth, args.codex_upstream, dict(os.environ), claude.url)
        session_id = args.session or uuid.uuid4().hex
        store = launcher.open_session(paths.contexto_home(), session_id, Path.cwd())
        proxy, engine_name = launcher.start_proxy(store, target, "codex", launcher.engine_config(options),
                                                  args.observe_thinking)
    except CodexLaunchError as exc:
        print(f"contexto: {exc}", file=stderr)
        return 2
    except launcher.LaunchError as exc:
        print(f"contexto: {exc}", file=stderr)
        return exc.code
    plan = None
    try:
        plan = plan_codex(list(args.cli_args), proxy.url, session_id, auth_mode=args.codex_auth,
                          key_helper=args.codex_key_helper, upstream=target)
        for note in plan.notes:
            print(f"contexto: {note}", file=stderr)
        print(f"contexto: session {session_id}, engine {engine_name}, codex auth {args.codex_auth}", file=stderr)
        return launcher.run_child(plan.argv, plan.env)
    except CodexLaunchError as exc:
        print(f"contexto: {exc}", file=stderr)
        return 2
    finally:
        if plan is not None:
            plan.cleanup()
        proxy.stop()
