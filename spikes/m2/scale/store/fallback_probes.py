"""The probe queries of D65: what ``contexto_search`` finds when the query is not the page's own words.

192 queries over the 6,000-page synthetic store of this spike (``scale_bench.build_store``, seed 7,
floor 0), in eight classes. ``fallback_bench.py`` runs them and reports recall, false-hit rate and
latency, before and after a change to search.

How the queries were written. I wrote the first 154 on 2026-11-12 from the fact's ``question`` and the
text of its plant in ``scale_probes.FACTS``, as a model would type them, before I ran any query
through search, and I have not changed a query after seeing its result. Two things were looked up
first, one word at a time and never a query: how many pages of the store hold a candidate word for the
``none`` class (its absent words must be on no page, its common word on many), and for the added word
of an ``extra_word`` probe. Nothing here is tuned to the fallback search this set measures: I did not
see that code.

After review (also 2026-11-12, after the first run on ``main``) 39 probes were added and one was taken
out; no existing query was changed. The reviewers found that the first typo, extra-word and paraphrase
probes mostly ask one thing, "ignore a word that is on no page". The added probes (flag
``after_review``) ask the rest: 15 typos whose misspelled word carries the query, 11 extra words and 12
paraphrases whose off-page word is on many pages, and one ``none`` probe. They were written the same
way, from the facts and from single-word lookups: how many pages hold a word, and whether a fact's
page holds it. ``none-16`` is in ``EXCLUDED`` with the reason.

Classes (``MINIMUM`` has the least each must hold):

- ``exact``: words of the page itself; strict search (every word on the page) finds it today. The
  control: it must not move.
- ``identifier``: the page holds an identifier in one style and the query writes it in another
  (snake_case, camelCase, kebab-case, dotted). Case alone does not count: search already folds case.
  ``original`` is the identifier as the page writes it, ``altered`` as the query does. Three probes
  also hold other words of the page (flag ``page_words``), which alone can find the page.
- ``paraphrase``: the fact in other words. Some words are on the page, at least one is not. Probes 1
  to 30 are one per fact. Probes 31 to 42 (``sub_kind`` ``common_word``) replace one word of the page
  by a word that many pages hold (``altered``) and keep the page's other words.
- ``question``: a natural-language question, one per fact. Every third is the fact's own
  ``question`` (``sub_kind`` ``fact_question``); the rest are new ones in that style (``new``).
- ``typo``: words of the page with one single-character typo in a word of 6 or more characters
  (``sub_kind``: substitution, deletion, insertion, or swap of two adjacent characters; a swap counts
  as one edit here, and is two under plain Levenshtein distance). ``base`` is the query spelled right,
  ``original`` the word and ``altered`` the typo. Probes 1 to 15 are exact probe ``i`` with a typo:
  the other words are the page's own and often find it without the misspelled one. In probes 16 to 30
  (flag ``carries_query``) the misspelled word is the only word, or stands with one word that many
  pages hold. ``typo-14`` types ``woker``, 5 characters, for ``worker`` (flag ``short_typo``).
- ``extra_word``: exact probe ``i`` plus one word the expected page does not hold (``altered``).
  ``sub_kind`` ``no_page``: the word is on no page of the store; ``some_pages``: it is on other pages
  (5 to 1,885 of the 6,000).
- ``two_page``: one query naming two facts that sit on different pages; both pages are expected.
- ``none``: nothing should be found. ``sub_kind`` ``absent``: every word is on no page of the store.
  ``mixed``: a topic that is on no page (``absent``) with a common word that is on many (``timeout``
  is on 831 of the 6,000 pages, ``import`` on all of them).

``expected`` names each expected page as (fact id, plant index); ``expected_pages`` resolves it to a
page id through the list of ids a build returns. For a fact whose value changes it is the last plant,
the one that is right now.

The two halves. Every fact is in one half (``HELD_OUT_FACTS``, the rest in ``tune``), and every probe
is in the half of its fact. The two facts of a two-page probe share a half, and so do facts whose
expected plants sit on one page, so no page is asked about in both halves. ``none`` probes have no fact and alternate. Parameters of a search change are
chosen on ``tune`` and reported on ``held_out``. Both halves come from one synthetic corpus and one
author, so the held-out half shows whether a setting carries over to other facts and other queries of
the same kind, and nothing more.

The corpus. The default plants write identifiers in snake_case, UPPER_SNAKE, two PascalCase names and
two kebab-case keys; no page holds a camelCase or dotted identifier. ``EXTRA_FACTS`` adds six pages
that do (two camelCase, two dotted, two kebab-case). They are planted only when a build is given
``extra=EXTRA_FACTS``; without it the corpus is byte for byte the one D34, D39 and D41 were measured
on (``test_fallback_probes.py`` checks that). The D65 run uses the extended corpus: 6 of the 6,000
pages differ from the default one, and no page of a ``scale_probes`` fact is among them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from scale_probes import FACTS, Fact, Plant, planted_turn

CLASSES = ("exact", "identifier", "paraphrase", "question", "typo", "extra_word", "two_page", "none")
MINIMUM = {"exact": 15, "identifier": 15, "paraphrase": 15, "question": 15, "typo": 15, "extra_word": 15,
           "two_page": 10, "none": 15}

# Pages that hold a camelCase, dotted, or kebab-case identifier (see the module docstring). No queries,
# no distractors: only the planted page holds each identifier.
EXTRA_FACTS: tuple[Fact, ...] = (
    Fact("x01", "static", "Where does the web client use the refresh token?", (),
         (Plant(0.03, "const refreshToken = await auth.rotate(stored.refreshToken);",
                assistant="The web client keeps the login alive with refreshToken: it calls rotate() about a "
                          "minute before the access token expires.",
                tool_result="  const refreshToken = await auth.rotate(stored.refreshToken);"),)),
    Fact("x02", "static", "How many connections may the client open?", (),
         (Plant(0.27, '"maxConnections": 40',
                assistant="maxConnections is 40 in conf/db.json, well under the limit of 100 the server "
                          "allows.",
                tool_result='  "maxConnections": 40,'),)),
    Fact("x03", "static", "How long does the client wait for a full fetch?", (),
         (Plant(0.42, "fetch.max.wait.ms=250",
                assistant="The client waits up to 250 ms for a full fetch (fetch.max.wait.ms), which explains "
                          "the idle gaps in the trace.",
                tool_result="fetch.max.wait.ms=250"),)),
    Fact("x04", "static", "What is the lowest TLS version the gateway accepts?", (),
         (Plant(0.64, "tls.min.version=1.3",
                assistant="Raised tls.min.version to 1.3 in gateway.properties; older clients are refused at the "
                          "handshake.",
                tool_result="tls.min.version=1.3"),)),
    Fact("x05", "static", "Which header carries the trace across services?", (),
         (Plant(0.76, "x-request-id: 7f3e2a9c41d0b865",
                assistant="The gateway now forwards x-request-id to every upstream, so one trace can be "
                          "followed across services.",
                tool_result="x-request-id: 7f3e2a9c41d0b865"),)),
    Fact("x06", "static", "How long does a pod drain before it stops?", (),
         (Plant(0.93, "drain-grace-period: 45s",
                assistant="Set drain-grace-period to 45s in the deploy manifest so in-flight exports finish "
                          "before the pod stops.",
                tool_result="    drain-grace-period: 45s"),)),
)


@dataclass(frozen=True)
class Probe:
    id: str
    cls: str  # one of CLASSES
    query: str
    expected: tuple[tuple[str, int], ...]  # (fact id, plant index) of each expected page; none: empty
    original: str = ""  # typo: the word spelled right; identifier: the identifier as the page writes it
    altered: str = ""  # typo, identifier: that word as the query writes it; extra_word: the added word
    base: str = ""  # typo, extra_word: the exact query it was made from
    sub_kind: str = ""  # typo: the edit; identifier: "page style>query style"; others: see the docstring
    absent: tuple[str, ...] = ()  # none: the words of the query that are on no page
    flags: tuple[str, ...] = ()  # see FLAGS


def _last(fact_id: str) -> tuple[str, int]:
    fact = next(f for f in (*FACTS, *EXTRA_FACTS) if f.id == fact_id)
    return fact_id, len(fact.plants) - 1


def style_of(identifier: str) -> str:
    """snake, kebab, dotted or camel (PascalCase counts as camel); ValueError for a mix or a plain word."""
    seps = {c for c in identifier if c in "_-."}
    if len(seps) == 1:
        return {"_": "snake", "-": "kebab", ".": "dotted"}[seps.pop()]
    if not seps and re.search(r"[a-z0-9][A-Z]", identifier):
        return "camel"
    raise ValueError(f"{identifier!r} is not written in one identifier style")


def _exact(n: int, fact_id: str, query: str) -> Probe:
    return Probe(f"exact-{n:02d}", "exact", query, (_last(fact_id),))


def _identifier(n: int, fact_id: str, original: str, altered: str, query: str = "") -> Probe:
    return Probe(f"identifier-{n:02d}", "identifier", query or altered, (_last(fact_id),), original=original,
                 altered=altered, sub_kind=f"{style_of(original)}>{style_of(altered)}",
                 flags=("page_words",) if query else ())


def _paraphrase(n: int, fact_id: str, query: str, common: str = "") -> Probe:
    """``common`` (probes added after review): the substituted word, which is on many pages."""
    return Probe(f"paraphrase-{n:02d}", "paraphrase", query, (_last(fact_id),), altered=common,
                 sub_kind="common_word" if common else "", flags=("after_review",) if common else ())


def _question(n: int, fact_id: str, query: str, own: bool = False) -> Probe:
    return Probe(f"question-{n:02d}", "question", query, (_last(fact_id),),
                 sub_kind="fact_question" if own else "new")


def _typo(n: int, base: Probe, original: str, altered: str, edit: str, *flags: str) -> Probe:
    query = " ".join(altered if w == original else w for w in base.query.split())
    return Probe(f"typo-{n:02d}", "typo", query, base.expected, original=original, altered=altered,
                 base=base.query, sub_kind=edit, flags=flags)


def _carried(n: int, fact_id: str, base: str, original: str, altered: str, edit: str) -> Probe:
    """A typo probe added after review: ``base`` is words of the page (one word, or a rare word and a
    common one), and the misspelled word is the one that tells the page from the others."""
    return _typo(n, Probe("", "exact", base, (_last(fact_id),)), original, altered, edit, "carries_query",
                 "after_review")


def _extra(n: int, base: Probe, query: str, word: str, kind: str = "no_page") -> Probe:
    """``kind``: ``no_page`` when the added word is on no page of the store, ``some_pages`` otherwise."""
    return Probe(f"extra_word-{n:02d}", "extra_word", query, base.expected, altered=word, base=base.query,
                 sub_kind=kind, flags=("after_review",) if n > 15 else ())


def _two(n: int, first: str, second: str, query: str) -> Probe:
    return Probe(f"two_page-{n:02d}", "two_page", query, (_last(first), _last(second)))


def _none(n: int, query: str, absent: tuple[str, ...]) -> Probe:
    kind = "absent" if len(absent) == len(query.split()) else "mixed"
    return Probe(f"none-{n:02d}", "none", query, (), sub_kind=kind, absent=absent)


EXACT: tuple[Probe, ...] = (
    _exact(1, "s01", "staging DB port"),
    _exact(2, "s08", "Redis eviction policy"),
    _exact(3, "s11", "webhook signing secret"),
    _exact(4, "s05", "public API rate limit"),
    _exact(5, "s07", "feature flag streaming path"),
    _exact(6, "s09", "release branch cut"),
    _exact(7, "s13", "owns payments service"),
    _exact(8, "s16", "regression first bad commit"),
    _exact(9, "s12", "nightly backups cron"),
    _exact(10, "s10", "max upload size"),
    _exact(11, "s06", "CSV export bug ticket"),
    _exact(12, "r01", "Renamed parse_header"),
    _exact(13, "c01", "HTTP client timeout"),
    _exact(14, "c02", "worker pool size"),
    _exact(15, "c05", "deploy region"),
)

# Probes 3, 6, 9, 12, 15 and 18 are on the EXTRA_FACTS pages.
IDENTIFIER: tuple[Probe, ...] = (
    _identifier(1, "s07", "stream_chunked_v3", "streamChunkedV3"),
    _identifier(2, "r01", "decode_frame_header", "decodeFrameHeader"),
    _identifier(3, "x01", "refreshToken", "refresh_token"),
    _identifier(4, "r03", "fetch_rows", "fetchRows", "replaced fetchRows"),
    _identifier(5, "c07", "ttl_seconds", "ttlSeconds"),
    _identifier(6, "x02", "maxConnections", "max-connections"),
    _identifier(7, "s18", "cidr_block", "cidrBlock"),
    _identifier(8, "r04", "retry_backoff_ms", "retry.backoff.ms"),
    _identifier(9, "x03", "fetch.max.wait.ms", "fetchMaxWaitMs"),
    _identifier(10, "s10", "MAX_UPLOAD_BYTES", "maxUploadBytes"),
    _identifier(11, "s11", "WEBHOOK_HMAC_KEY", "webhook-hmac-key"),
    _identifier(12, "x04", "tls.min.version", "tls_min_version"),
    _identifier(13, "c08", "MAX_RETRIES", "maxRetries", "maxRetries uploader"),
    _identifier(14, "c05", "DEPLOY_REGION", "deploy-region"),
    _identifier(15, "x05", "x-request-id", "xRequestId"),
    _identifier(16, "c06", "LOG_LEVEL", "log.level"),
    _identifier(17, "s01", "STAGING_DB_PORT", "staging.db.port"),
    _identifier(18, "x06", "drain-grace-period", "drain.grace.period"),
    _identifier(19, "s03", "artifacts_bucket", "artifactsBucket"),
    _identifier(20, "c04", "BATCH_SIZE", "batch-size"),
    _identifier(21, "s08", "maxmemory-policy", "maxmemory_policy"),
    _identifier(22, "s15", "requires-python", "requires_python"),
    _identifier(23, "r02", "UserCache", "user_cache", "user_cache old name"),
)

PARAPHRASE: tuple[Probe, ...] = (
    _paraphrase(1, "s01", "staging database port"),
    _paraphrase(2, "s02", "intermittently failing test"),
    _paraphrase(3, "s03", "build artifacts S3 bucket"),
    _paraphrase(4, "s04", "password hash algorithm"),
    _paraphrase(5, "s05", "API throttle requests per minute"),
    _paraphrase(6, "s06", "customer ticket CSV export issue"),
    _paraphrase(7, "s07", "streaming path feature toggle"),
    _paraphrase(8, "s08", "Redis maxmemory eviction setting"),
    _paraphrase(9, "s09", "release branch cutoff day"),
    _paraphrase(10, "s10", "maximum upload file size"),
    _paraphrase(11, "s11", "webhook HMAC secret environment variable"),
    _paraphrase(12, "s12", "nightly backup schedule"),
    _paraphrase(13, "s13", "payments service maintainer"),
    _paraphrase(14, "s14", "E4127 error meaning"),
    _paraphrase(15, "s15", "minimum supported Python version"),
    _paraphrase(16, "s16", "regression culprit commit"),
    _paraphrase(17, "s17", "canary failure on-call channel"),
    _paraphrase(18, "s18", "VPC address range"),
    _paraphrase(19, "r01", "parse_header replacement function"),
    _paraphrase(20, "r02", "UserCache class renamed"),
    _paraphrase(21, "r03", "fetch_rows streaming replacement"),
    _paraphrase(22, "r04", "retry_backoff_ms deprecated config key"),
    _paraphrase(23, "c01", "HTTP request timeout seconds"),
    _paraphrase(24, "c02", "worker pool concurrency"),
    _paraphrase(25, "c03", "locked upstream API version"),
    _paraphrase(26, "c04", "ingestion batch size"),
    _paraphrase(27, "c05", "deployment region"),
    _paraphrase(28, "c06", "root logger verbosity level"),
    _paraphrase(29, "c07", "cache expiry seconds"),
    _paraphrase(30, "c08", "uploader retry count"),
    # Added 2026-11-12 after review: one word of the page is replaced by a word that is on many pages
    # and not on the expected one; the other words are the page's own (pages at 6,000: timeout 831,
    # cache 267, name 249, rename 248, limit 131, policy 77, job 72, workers 42, version 38).
    _paraphrase(31, "c07", "cache timeout seconds", "timeout"),
    _paraphrase(32, "s02", "flaky test name", "name"),
    _paraphrase(33, "s03", "artifacts bucket name", "name"),
    _paraphrase(34, "r03", "fetch_rows rename", "rename"),
    _paraphrase(35, "r02", "UserCache rename", "rename"),
    _paraphrase(36, "s08", "Redis cache eviction", "cache"),
    _paraphrase(37, "s10", "upload size limit", "limit"),
    _paraphrase(38, "c08", "uploader retries limit", "limit"),
    _paraphrase(39, "s12", "nightly backup job", "job"),
    _paraphrase(40, "s04", "password hashing policy", "policy"),
    _paraphrase(41, "c02", "pool size workers", "workers"),
    _paraphrase(42, "s15", "minimum Python version", "version"),
)

QUESTION: tuple[Probe, ...] = (
    _question(1, "s01", "What port is the staging database on?", own=True),
    _question(2, "s02", "Which test in the ledger suite fails intermittently?"),
    _question(3, "s03", "What is the name of the bucket the artifacts go to?"),
    _question(4, "s04", "Which password hashing algorithm did we decide on?", own=True),
    _question(5, "s05", "How many requests per minute does the public API allow per key?"),
    _question(6, "s06", "Which ticket did the customer open for the CSV export bug?"),
    _question(7, "s07", "What is the feature flag for the new streaming path?", own=True),
    _question(8, "s08", "Which eviction policy is Redis using now?"),
    _question(9, "s09", "What day and time does the release branch get cut?"),
    _question(10, "s10", "What is the max upload size?", own=True),
    _question(11, "s11", "Which environment variable holds the webhook signing secret?"),
    _question(12, "s12", "What time do the nightly backups run from cron?"),
    _question(13, "s13", "Who owns the payments service?", own=True),
    _question(14, "s14", "Why does the router report E4127?"),
    _question(15, "s15", "Which Python versions do we still support?"),
    _question(16, "s16", "Which commit introduced the regression?", own=True),
    _question(17, "s17", "Where should I post if the canary fails?"),
    _question(18, "s18", "What is the CIDR range of the VPC?"),
    _question(19, "r01", "What did we rename parse_header to?", own=True),
    _question(20, "r02", "What was UserCache renamed to?"),
    _question(21, "r03", "What replaced fetch_rows in the export?"),
    _question(22, "r04", "What is the retry_backoff_ms setting called now?", own=True),
    _question(23, "c01", "How many seconds is the HTTP client timeout set to?"),
    _question(24, "c02", "How many workers does the worker pool have now?"),
    _question(25, "c03", "Which upstream API version is pinned?", own=True),
    _question(26, "c04", "How large is the ingest batch now?"),
    _question(27, "c05", "Which region are we deploying to now?"),
    _question(28, "c06", "What is the root log level?", own=True),
    _question(29, "c07", "How long is the cache TTL in seconds?"),
    _question(30, "c08", "How many retries does the uploader make?"),
)

# Typo i (1 to 15) is exact probe i with one edit.
TYPO: tuple[Probe, ...] = (
    _typo(1, EXACT[0], "staging", "stagng", "deletion"),
    _typo(2, EXACT[1], "policy", "polciy", "swap"),
    _typo(3, EXACT[2], "secret", "seccret", "insertion"),
    _typo(4, EXACT[3], "public", "publoc", "substitution"),
    _typo(5, EXACT[4], "streaming", "straeming", "swap"),
    _typo(6, EXACT[5], "release", "relase", "deletion"),
    _typo(7, EXACT[6], "service", "servise", "substitution"),
    _typo(8, EXACT[7], "regression", "regresssion", "insertion"),
    _typo(9, EXACT[8], "backups", "bakups", "deletion"),
    _typo(10, EXACT[9], "upload", "uplaod", "swap"),
    _typo(11, EXACT[10], "ticket", "tickett", "insertion"),
    _typo(12, EXACT[11], "parse_header", "parse_heador", "substitution"),
    _typo(13, EXACT[12], "timeout", "timeuot", "swap"),
    _typo(14, EXACT[13], "worker", "woker", "deletion", "short_typo"),
    _typo(15, EXACT[14], "region", "regiom", "substitution"),
    # Added 2026-11-12 after review: the misspelled word carries the query. 16 to 23 are one word that
    # only the planted page holds. 24 to 30 are that kind of word with a word of the page that many
    # other pages hold too (pages at 6,000: flag 1,332, retries 329, bucket 204, python all, limit 131,
    # timeout 831, region 273).
    _carried(16, "s08", "allkeys-lfu", "allkeys-lfu", "allkeys-lru", "substitution"),
    _carried(17, "s06", "SUP-48213", "SUP-48213", "SUP-48231", "swap"),
    _carried(18, "s11", "WEBHOOK_HMAC_KEY", "WEBHOOK_HMAC_KEY", "WEBHOOK_HAMC_KEY", "swap"),
    _carried(19, "s07", "stream_chunked_v3", "stream_chunked_v3", "stream_chuncked_v3", "insertion"),
    _carried(20, "s02", "test_resume_after_crash", "test_resume_after_crash", "test_resume_after_crsh", "deletion"),
    _carried(21, "s04", "argon2id", "argon2id", "aragon2id", "insertion"),
    _carried(22, "s13", "Natarajan", "Natarajan", "Natarjan", "deletion"),
    _carried(23, "s18", "cidr_block", "cidr_block", "cidr_blcok", "swap"),
    _carried(24, "s07", "streaming flag", "streaming", "streamimg", "substitution"),
    _carried(25, "c08", "uploader retries", "uploader", "uploder", "deletion"),
    _carried(26, "s03", "artifacts bucket", "artifacts", "artefacts", "substitution"),
    _carried(27, "s15", "minimum python", "minimum", "minimun", "substitution"),
    _carried(28, "s04", "bcrypt limit", "bcrypt", "bcrpyt", "swap"),
    _carried(29, "c01", "client timeout", "client", "cliant", "substitution"),
    _carried(30, "c05", "deploy region", "deploy", "depoly", "swap"),
)

# Extra word i is exact probe i plus one word. Pages of the default 6,000-page store that hold the added
# word, looked up one word at a time before any query ran: config 9; number, production, location, quota,
# schedule, setting, current and latest 0. Not looked up: rollout, team, bisect, job, Jira, function.
# The bench reports the count for every probe (``altered_pages``).
EXTRA_WORD: tuple[Probe, ...] = (
    _extra(1, EXACT[0], "staging DB port number", "number"),
    _extra(2, EXACT[1], "Redis eviction policy production", "production"),
    _extra(3, EXACT[2], "webhook signing secret location", "location"),
    _extra(4, EXACT[3], "public API rate limit quota", "quota"),
    _extra(5, EXACT[4], "feature flag streaming path rollout", "rollout"),
    _extra(6, EXACT[5], "release branch cut schedule", "schedule"),
    _extra(7, EXACT[6], "owns payments service team", "team"),
    _extra(8, EXACT[7], "regression first bad commit bisect", "bisect", "some_pages"),
    _extra(9, EXACT[8], "nightly backups cron job", "job", "some_pages"),
    _extra(10, EXACT[9], "max upload size setting", "setting"),
    _extra(11, EXACT[10], "CSV export bug ticket Jira", "Jira"),
    _extra(12, EXACT[11], "Renamed parse_header function", "function"),
    _extra(13, EXACT[12], "current HTTP client timeout", "current"),
    _extra(14, EXACT[13], "worker pool size config", "config", "some_pages"),
    _extra(15, EXACT[14], "latest deploy region", "latest"),
    # Added 2026-11-12 after review: the added word is on many pages, and not on the expected one (pages
    # at 6,000, one word at a time: owner 1,885, flag 1,332, timeout 831, retries 329, limit 131,
    # policy 77, failover 66, client 48).
    _extra(16, EXACT[2], "webhook signing secret owner", "owner", "some_pages"),
    _extra(17, EXACT[3], "public API rate limit timeout", "timeout", "some_pages"),
    _extra(18, EXACT[9], "max upload size flag", "flag", "some_pages"),
    _extra(19, EXACT[10], "CSV export bug ticket owner", "owner", "some_pages"),
    _extra(20, EXACT[8], "nightly backups cron timeout", "timeout", "some_pages"),
    _extra(21, EXACT[12], "HTTP client timeout policy", "policy", "some_pages"),
    _extra(22, EXACT[5], "release branch cut policy", "policy", "some_pages"),
    _extra(23, EXACT[14], "deploy region failover", "failover", "some_pages"),
    _extra(24, EXACT[9], "max upload size limit", "limit", "some_pages"),
    _extra(25, EXACT[4], "feature flag streaming path client", "client", "some_pages"),
    _extra(26, EXACT[12], "HTTP client timeout retries", "retries", "some_pages"),
)

TWO_PAGE: tuple[Probe, ...] = (
    _two(1, "s01", "s08", "staging DB port Redis eviction policy"),
    _two(2, "s11", "s05", "webhook signing secret and API rate limit"),
    _two(3, "s07", "s10", "feature flag streaming path max upload size"),
    _two(4, "c01", "c08", "HTTP client timeout and max retries"),
    _two(5, "c02", "c04", "worker pool size ingest batch size"),
    _two(6, "s09", "s12", "release branch cut nightly backups"),
    _two(7, "r01", "r03", "Renamed parse_header replaced fetch_rows"),
    _two(8, "c05", "s18", "deploy region VPC cidr_block"),
    _two(9, "s13", "s04", "payments service and password hashing"),
    _two(10, "s14", "s16", "E4127 shard map first bad commit"),
)

# Two absent, two mixed, and so on; the probes alternate between the halves, so each holds four of
# each. Pages of the default 6,000-page
# store that hold the common word: timeout 831, flag 1,332, owner 1,885, region 273, retries 329,
# regression 355, import, error, session and token 6,000.
NONE: tuple[Probe, ...] = (
    _none(1, "kubernetes ingress annotations", ("kubernetes", "ingress", "annotations")),
    _none(2, "GraphQL resolver dataloader", ("GraphQL", "resolver", "dataloader")),
    _none(3, "kubernetes liveness probe timeout", ("kubernetes", "liveness", "probe")),
    _none(4, "GraphQL subscription flag", ("GraphQL", "subscription")),
    _none(5, "OAuth redirect callback", ("OAuth", "redirect", "callback")),
    _none(6, "Kafka consumer rebalance", ("Kafka", "consumer", "rebalance")),
    _none(7, "Stripe invoice owner", ("Stripe", "invoice")),
    _none(8, "Postgres replication region", ("Postgres", "replication")),
    _none(9, "docker multistage image", ("docker", "multistage", "image")),
    _none(10, "Postgres autovacuum bloat", ("Postgres", "autovacuum", "bloat")),
    _none(11, "websocket reconnect retries", ("websocket", "reconnect")),
    _none(12, "Prometheus alertmanager regression", ("Prometheus", "alertmanager")),
    _none(13, "websocket heartbeat reconnect", ("websocket", "heartbeat", "reconnect")),
    _none(14, "Stripe invoice proration", ("Stripe", "invoice", "proration")),
    _none(15, "numpy import error", ("numpy",)),
    _none(17, "webpack bundle import", ("webpack", "bundle")),  # added 2026-11-12 after review, for none-16
)

# Probes taken out of the set after review, with the reason. Their queries are kept as written.
EXCLUDED: tuple[tuple[Probe, str], ...] = (
    (_none(16, "OAuth session token", ("OAuth",)),
     "not nothing-to-find in the extended corpus: the added x01 page is about refresh and access tokens "
     "for a login, which is near this query"),
)

PROBES: tuple[Probe, ...] = tuple(
    replace(p, flags=(*p.flags, "after_review")) if p.id == "none-17" else p
    for p in (*EXACT, *IDENTIFIER, *PARAPHRASE, *QUESTION, *TYPO, *EXTRA_WORD, *TWO_PAGE, *NONE))

# What a flag on a probe says. The bench reports recall with and without each.
FLAGS = {
    "after_review": "added on 2026-11-12 after the review of the first set",
    "carries_query": "typo: the misspelled word is the only word, or the only rare one",
    "short_typo": "typo: the misspelled word is under 6 characters as typed (the right spelling has 6)",
    "page_words": "identifier: the query also holds other words of the page",
}

_FACTS = {f.id: f for f in (*FACTS, *EXTRA_FACTS)}

# The facts of the held-out half; every other fact is in tune. Fixed here, so adding a probe moves no
# fact. Chosen once (2026-11-12) so that facts which must share a half do (the two facts of a two-page
# probe, and facts whose expected plants sit on one page: s05 with c04, s14 with c03), and each class,
# each kind of fact, each typo edit, the probes added after review and the six added pages are as even
# as that allows. Both pages with a dotted identifier (x03, x04) came out in tune.
HELD_OUT_FACTS: frozenset[str] = frozenset({
    "s01", "s02", "s07", "s08", "s10", "s14", "s16", "s17", "s18", "r02", "c01", "c03", "c05", "c07", "c08",
    "x02", "x05", "x06"})
_NONE_HALF = {p.id: ("tune" if i % 2 == 0 else "held_out") for i, p in enumerate(NONE)}


def split_of(probe: Probe) -> str:
    """``tune`` or ``held_out``. Every probe of a fact is in that fact's half, so no page is asked about
    in both; a two-page probe is in the half its two facts share. ``none`` probes have no fact and
    alternate, the first in ``tune``."""
    if not probe.expected:
        return _NONE_HALF[probe.id]
    halves = {"held_out" if fact_id in HELD_OUT_FACTS else "tune" for fact_id, _ in probe.expected}
    if len(halves) != 1:
        raise ValueError(f"{probe.id} names facts of both halves")
    return halves.pop()


def fact_of(fact_id: str) -> Fact:
    """The fact of ``scale_probes.FACTS`` or ``EXTRA_FACTS`` with this id."""
    return _FACTS[fact_id]


def common_words(probe: Probe) -> list[str]:
    """The words of a ``none`` probe that are not in its ``absent`` words (empty for sub-kind ``absent``)."""
    return [w for w in probe.query.split() if w not in probe.absent]


def expected_turns(probe: Probe, n: int) -> list[int]:
    """The turn (0-based page position) of each expected page in an ``n``-page store."""
    return [planted_turn(_FACTS[fact_id].plants[plant].at, n) for fact_id, plant in probe.expected]


def expected_pages(probe: Probe, ids: list[str]) -> list[str]:
    """The id of each expected page; ``ids`` is the list a build returns, one id per turn, oldest first."""
    return [ids[t] for t in expected_turns(probe, len(ids))]


def plant_text(fact_id: str, plant: int) -> str:
    """Everything one plant puts on its page: prompt, reply, tool output line, and both strings of an edit."""
    p = _FACTS[fact_id].plants[plant]
    return "\n".join([p.user, p.assistant, p.tool_result, *(p.edit or ())])


def planted_text(probe: Probe, n: int) -> str:
    """Everything planted on the probe's expected pages in an ``n``-page store, other facts' plants that
    land on the same page included (two plants share a fraction of the session, so share a page at every n)."""
    turns = set(expected_turns(probe, n))
    return "\n".join(plant_text(f.id, i) for f in _FACTS.values() for i, p in enumerate(f.plants)
                     if planted_turn(p.at, n) in turns)
