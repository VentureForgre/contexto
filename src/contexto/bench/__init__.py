"""``contexto bench``: a benchmark of Contexto against full context and plain Claude Code (D66).

The pieces, each importable on its own and standard library only:

- ``contexto.bench.script``: the session script generator (protocols ``d27`` and ``v2``), its
  self-checks and the deterministic scoring.
- ``contexto.bench.stats``: the statistics the report uses, as pure functions.
- ``contexto.bench.modes``: one module per mode of the ``contexto bench`` command.

Nothing here imports from the repository's ``spikes/`` tree, which the wheel does not ship. The
generator code the benchmark needs is ported into ``contexto.bench.d27``, and
``tests/test_bench_script.py`` proves the port produces the same bytes as the spike.
"""
