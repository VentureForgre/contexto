"""The options the live run and ``--dry-run`` share, defined once, and the run configuration they give."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from contexto.bench.modes import add_option, seeds

DEFAULT_UPSTREAM = "https://api.anthropic.com"
TURN_REQUEST_CAP = 60  # contexto.bench.runner.TURN_REQUEST_CAP, repeated so that module is not imported at start
ARMS = ("contexto", "full", "plain")  # contexto.bench.arms.ARMS, repeated so that module is not imported at start


def _arms(text: str) -> tuple[str, ...]:
    names = [x.strip() for x in text.split(",") if x.strip()]
    bad = [n for n in names if n not in ARMS]
    if bad or not names or len(set(names)) != len(names):
        raise argparse.ArgumentTypeError(f"invalid arms {text!r}: use a comma-separated list of {', '.join(ARMS)}")
    return tuple(a for a in ARMS if a in names)


def _tokens(text: str) -> int:
    cleaned = text.replace(",", "").replace("_", "")
    if not (cleaned.isascii() and cleaned.isdecimal()) or int(cleaned) == 0:
        raise argparse.ArgumentTypeError(f"invalid token count {text!r}: use a whole number, 1 or more")
    return int(cleaned)


def _seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = 0.0
    if not value > 0:
        raise argparse.ArgumentTypeError(f"invalid time {text!r}: use a number of seconds above 0")
    return value


def add(parser: Any) -> None:
    add_option(parser, "--model", default=None, metavar="M",
               help="the model every arm runs, for example claude-haiku-5-5 (the live run needs it)")
    add_option(parser, "--arms", type=_arms, default=ARMS, metavar="LIST",
               help="the arms to run, comma separated (default contexto,full,plain)")
    add_option(parser, "--budget-tokens", type=_tokens, default=None, metavar="T",
               help="the most tokens the run may use, input and output of every arm together, counting what an "
                    "earlier run already recorded in OUT. The live run refuses to start without it or when the "
                    "plan's worst case is above it, and stops before passing it; --dry-run prints the figure to pass")
    add_option(parser, "--upstream", default=None, metavar="URL",
               help=f"the model API's base URL (default $ANTHROPIC_BASE_URL when it is set, else {DEFAULT_UPSTREAM}; "
                    "inside a Claude Code or Contexto session that variable may name a local proxy). The manifest "
                    "records only its kind and where it came from, never the host; a run against 127.0.0.1 is "
                    "labelled a stub run")
    add_option(parser, "--key-helper", default=None, metavar="COMMAND",
               help="a command that prints your API key, run with /bin/sh -c when a key is needed, as Claude Code "
                    "runs an apiKeyHelper. Without it and without --settings the key is $ANTHROPIC_API_KEY. A key "
                    "is never given on the command line")
    add_option(parser, "--settings", type=Path, default=None, metavar="FILE",
               help="a Claude Code settings file whose apiKeyHelper prints your API key")
    add_option(parser, "--allow-git-out", action="store_true",
               help="allow OUT inside a git work tree (refused by default, so results are not committed by accident)")
    add_option(parser, "--work", type=Path, default=None, metavar="DIR",
               help="where the units' working directories are made: Claude Code's and Contexto's session files, "
                    "removed after each unit (default: a new temporary directory). It must be outside any git "
                    "work tree")
    add_option(parser, "--keep-work", action="store_true", help="keep the units' working directories")
    add_option(parser, "--cache-ttl", choices=("5m", "1h"), default="5m",
               help="the prompt cache lifetime both Claude Code arms are told to use "
                    "(CLAUDE_CODE_PROMPT_CACHE_TTL) and the full arm marks its history with (default 5m)")
    add_option(parser, "--turn-timeout", type=_seconds, default=1_800.0, metavar="SECONDS",
               help="stop a unit when a turn has no result for this long (default 1800)")
    add_option(parser, "--max-turn-requests", type=_tokens, default=TURN_REQUEST_CAP, metavar="N",
               help="stop a unit as runaway when one script turn asks for more than N billed requests (default "
                    f"{TURN_REQUEST_CAP}; the recorded runs needed at most 6, the tool-loop turn needs 11); the run "
                    "goes on with the next unit")
    add_option(parser, "--max-unit-tokens", type=_tokens, default=None, metavar="T",
               help="stop a unit as runaway when it would pass T tokens (default: twice the unit's planned worst "
                    "case); the run goes on with the next unit")
    add_option(parser, "--context-window", type=_tokens, default=None, metavar="TOKENS",
               help="the model's context window, for the full arm's fit check (default: the published window of "
                    "a model the benchmark knows, else 200,000)")


def upstream(args: argparse.Namespace) -> str:
    return args.upstream or os.environ.get("ANTHROPIC_BASE_URL") or DEFAULT_UPSTREAM


def upstream_source(args: argparse.Namespace) -> str:
    """Where the upstream came from: ``flag``, ``environment`` or ``default``."""
    if args.upstream:
        return "flag"
    return "environment" if os.environ.get("ANTHROPIC_BASE_URL") else "default"


def config(args: argparse.Namespace, credentials: Any) -> Any:
    from contexto.bench.runner import Config

    return Config(model=args.model or "", bound=args.bound, protocol=args.protocol, turn_kbytes=args.turn_kbytes,
                  seed=args.seed, seeds=seeds(args), repeats=args.repeats, arms=tuple(args.arms),
                  budget=args.budget_tokens, out=args.out, upstream=upstream(args), credentials=credentials,
                  work=args.work, keep_work=args.keep_work, cache_ttl=args.cache_ttl,
                  turn_timeout=args.turn_timeout, context_window=args.context_window,
                  upstream_source=upstream_source(args), turn_request_cap=args.max_turn_requests,
                  unit_token_cap=args.max_unit_tokens)
