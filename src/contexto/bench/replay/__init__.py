"""``contexto bench --replay``: a keyless replay of a synthetic session through the real proxy (D66).

A replay needs no key, no ``claude`` and no network. It regenerates a synthetic session from a seed
(``contexto.bench.script.build``), starts the real proxy with the rewrite engine on 127.0.0.1, the
way ``contexto run`` starts it, and drives it with a synthetic client against a local fake upstream
that never forwards anything. The fake upstream plays a scripted "model": it knows what to reply to
every turn because the script says so, and it reports a usage it computes from the bytes of each
request at stated rates.

What a replay shows: whether the proxy kept every request of this session under the bound, that every
request was one the Messages API accepts and none carried a compaction summary or marker, the size of
every request, and which planted facts the store's search can reach. What it cannot show: answer
accuracy, cache reads, real token counts (there is no tokenizer without a model, so every size is an
estimate), what a real CLI or a real model does, and what the proxy does with what only Claude Code
sends. Every environment variable the package reads is held at its default for the length of a
replay (``session.pinned_environment``).

Modules:

``trace``
    The pinned session: its parameters, the SHA-256 of every turn and file, the scripted model
    behaviour and the measurements a replay of it must give. The shipped trace is
    ``contexto/bench/data/trace-<seed>.json``; ``trace.record`` writes it, so it is never edited by
    hand.
``scripted``
    The scripted model: one reply per model round, derived from the script alone.
``upstream``
    The local fake upstream: it binds 127.0.0.1 only, has no client code, prices each request, and
    refuses with the API's 400 a request that breaks a rule of the Messages API.
``client``
    The synthetic client: a head (system prompt and tool definitions) of realistic size made of
    generated filler, cumulative messages, one request per model round, and the tool results.
``session``
    One replay: proxy, fake upstream and client together, and what was measured.
``reach``
    The fixed query rule per probe class, the best query from each question's own words beside it,
    and the search-reachability measurement: a floor under mechanical queries, not what a model can
    reach.
``lines``
    The two modelled comparison lines of the request-size chart: full context, and plain Claude
    Code compacting at the bound. They are computed from the script, not run.
``output``
    ``manifest.json``, ``requests.jsonl``, ``reach.jsonl``, ``results.json``, ``results.md`` and the
    text printed to the terminal.

Everything here is standard library only. ``contexto.bench.modes.replay`` imports this package inside
its ``run``, so starting any other ``contexto`` command does not load the proxy for it.
"""
