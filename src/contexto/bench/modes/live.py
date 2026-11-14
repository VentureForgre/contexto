"""``contexto bench`` with no mode option: the live run. It calls the model with your own key.

For each seed (and each repeat of the first) it runs the arms one after another on the same generated
script: ``contexto`` (Claude Code under ``contexto run``), ``full`` (direct requests carrying the
whole script) and ``plain`` (Claude Code with its own auto-compaction set from the bound). It writes
``manifest.json``, ``scripts/``, ``probes.jsonl`` and ``requests.jsonl`` into ``--out``; see
``contexto.bench.runner``. Nothing in those files or in what is printed names the key, the key
helper, a settings file, the upstream's host or a home directory.

It refuses to start without ``--model``, ``--out``, a credential and ``--budget-tokens`` at or above
the plan's worst case (``contexto bench --dry-run`` prints it), and with ``--out`` inside a git work
tree unless ``--allow-git-out`` is given.
"""

from __future__ import annotations

import argparse
import sys

from contexto.bench.modes import _live_options

FLAG = None
# The live run's options that --dry-run does not use, so they are refused beside any mode flag (the
# contract in contexto.bench.modes); this mode adds them itself. --model, --arms and --budget-tokens are
# not listed: the dry run plans with them, so both add them with add_option, and the plan of a live
# command is that command with --dry-run and without these.
OWN_OPTIONS = ("--upstream", "--key-helper", "--settings", "--allow-git-out", "--work", "--keep-work", "--cache-ttl",
               "--turn-timeout", "--max-turn-requests", "--max-unit-tokens", "--context-window")


def add_arguments(parser) -> None:
    _live_options.add_shared(parser)
    _live_options.add_live(parser)


def run(args: argparse.Namespace) -> int:
    from contexto.bench import credentials as bench_credentials
    from contexto.bench import runner

    def refuse(message: str) -> int:
        print(f"contexto bench: refusing: {message}", file=sys.stderr)
        return runner.EXIT_USAGE

    if not args.model:
        return refuse("the live run needs --model (for another mode, give its option, for example --dry-run)")
    if args.out is None:
        return refuse("the live run needs --out DIR")
    if not args.allow_git_out and runner.inside_git(args.out):
        return refuse("--out is inside a git work tree, where results could be committed before anyone has looked "
                      "at them; use a directory outside the repository, or pass --allow-git-out")
    try:
        credentials = bench_credentials.resolve(args.key_helper, args.settings)
    except bench_credentials.CredentialError as exc:
        return refuse(str(exc))
    return runner.run(_live_options.config(args, credentials))
