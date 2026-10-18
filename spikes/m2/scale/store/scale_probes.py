"""The fixed fact plants and probe queries of the store-scale spike (zero spend, deterministic).

Thirty facts are planted at fixed fractions of the session, so the same fact sits at the same
relative position at every store size N. Each fact has 2 or 3 queries, written as a model would
type them from the question alone, before any result was looked at (2026-10-17); they are never
tuned to the results. Search is an exact, case-sensitive substring match, so a query that is not
literally on the planted page cannot find it.

Kinds:

- ``static``: one value, stated once.
- ``rename``: an identifier renamed once. Turns before the rename mention the old name in code at
  ``RENAME_MENTION_RATE``, turns after it the new name; the probe asks what the old name became.
- ``changing``: a value set three times; only the last is correct, so the probe also records
  whether the latest value is the first one the reply shows.

``near`` sentences are distractors: realistic lines that share the query's words but not the fact.
Each query gets a per-page rate (see ``scale_synth.distractor_rate``), so the number of other pages a
query matches grows with N the way it would in one long session.

A ``Plant`` puts text in up to four places of a turn: the user's prompt, an assistant text block, a
line inside a tool result (file content or command output), and an Edit tool call (old and new
string). ``marker`` is a string present only on the planted page (checked by the harness), used to
tell whether a reply shows the value itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field

RENAME_MENTION_RATE = 0.04  # share of turns that touch a renamed identifier's file (assumption)


@dataclass(frozen=True)
class Plant:
    at: float  # fraction of the session, 0 = oldest page, 1 = newest
    marker: str  # on the planted page only
    user: str = ""
    assistant: str = ""
    tool_result: str = ""
    edit: tuple[str, str] | None = None  # (old_string, new_string) of an Edit call


@dataclass(frozen=True)
class Fact:
    id: str
    kind: str  # static, rename, changing
    question: str
    queries: tuple[str, ...]
    plants: tuple[Plant, ...]  # changing: oldest first; the last is correct
    near: tuple[str, ...] = ()
    rename: tuple[str, str] | None = None  # (old, new) identifiers mentioned in code around a rename
    extra: dict = field(default_factory=dict)


FACTS: tuple[Fact, ...] = (
    Fact("s01", "static", "What port is the staging database on?",
         ("staging DB port", "staging database", "DB port"),
         (Plant(0.37, "staging DB port is 6417",
                assistant="Found it: the staging DB port is 6417, not the default 5432, which is why the "
                          "integration job could not connect.",
                tool_result="STAGING_DB_PORT=6417"),),
         near=("The staging DB port mapping is defined in deploy/compose.staging.yml.",
               "Forwarded the staging database through the bastion for the migration check.",
               "the DB port is taken from the environment, not hard-coded")),
    Fact("s02", "static", "Which test was flaky?",
         ("flaky", "flaky test", "fails intermittently"),
         (Plant(0.12, "test_resume_after_crash in tests/test_ledger.py",
                assistant="The flaky one is test_resume_after_crash in tests/test_ledger.py; it fails about "
                          "1 in 20 runs because the fsync mock races the writer thread."),),
         near=("Re-ran the suite twice to rule out a flaky test before touching the code.",
               "That failure looked flaky but it reproduces every time with -p no:randomly.",
               "CI marks this job as flaky when it retries")),
    Fact("s03", "static", "What is the artifacts S3 bucket called?",
         ("artifacts bucket", "S3 bucket", "bucket"),
         (Plant(0.58, "ctx-artifacts-euw3-7f2a",
                assistant="The artifacts bucket is ctx-artifacts-euw3-7f2a (eu-west-3).",
                tool_result='artifacts_bucket = "ctx-artifacts-euw3-7f2a"'),),
         near=("Upload goes to the S3 bucket named in the release config.",
               "bucket policy denies public reads, as expected",
               "The artifacts bucket name is read from terraform output, not from the code.")),
    Fact("s04", "static", "Which password hashing algorithm did we decide on?",
         ("password hashing", "argon2", "hashing algorithm"),
         (Plant(0.21, "argon2id for password hashing",
                user="Let's go with argon2id for password hashing, bcrypt's 72-byte limit bit us before."),),
         near=("password hashing is done in auth/passwords.py and never logged",
               "Compared the hashing algorithm names in the audit log against the allowlist.")),
    Fact("s05", "static", "What is the public API rate limit?",
         ("rate limit", "requests per minute", "rate-limit"),
         (Plant(0.66, "340 requests per minute per key",
                assistant="Public API rate limit: 340 requests per minute per key, enforced at the gateway."),),
         near=("The client backs off on a 429 from the rate limit middleware.",
               "rate-limit headers are passed through unchanged",
               "the dashboard shows requests per minute per route")),
    Fact("s06", "static", "What was the customer's ticket number for the export bug?",
         ("export bug", "ticket", "CSV export"),
         (Plant(0.08, "SUP-48213",
                user="Customer escalated the CSV export bug, ticket SUP-48213. Can you look at the quoting "
                     "of embedded newlines first?"),),
         near=("CSV export writes the header row even for an empty result.",
               "Linked the ticket in the PR description.",
               "the export bug from last week is unrelated to this one")),
    Fact("s07", "static", "What is the feature flag for the new streaming path?",
         ("feature flag", "streaming path", "flag"),
         (Plant(0.44, "stream_chunked_v3",
                assistant="The new streaming path is gated behind the feature flag stream_chunked_v3, off by "
                          "default.",
                tool_result='FLAGS["stream_chunked_v3"] = False  # new streaming path'),),
         near=("Every feature flag is read once at startup from flags.toml.",
               "the old streaming path is kept for the fallback",
               "added a --flag to skip the slow fixtures")),
    Fact("s08", "static", "What Redis eviction policy did we pick?",
         ("eviction policy", "maxmemory-policy", "eviction"),
         (Plant(0.73, "allkeys-lfu",
                assistant="Switched the Redis eviction policy to allkeys-lfu; the hot keys survive now.",
                tool_result="maxmemory-policy allkeys-lfu"),),
         near=("The eviction counter in INFO stats stays at zero during the load test.",
               "eviction policy changes need a restart in the managed tier")),
    Fact("s09", "static", "When is the release branch cut?",
         ("release branch", "cut every", "code freeze"),
         (Plant(0.29, "every Thursday 14:00 UTC",
                user="Reminder: the release branch gets cut every Thursday 14:00 UTC, so anything for this "
                     "week has to land before then."),),
         near=("Cherry-picked the fix onto the release branch.",
               "the release branch protection requires two approvals")),
    Fact("s10", "static", "What is the max upload size?",
         ("max upload size", "MAX_UPLOAD", "upload limit"),
         (Plant(0.51, "MAX_UPLOAD_BYTES = 75 * 1024 * 1024",
                assistant="Raised the max upload size to 75 MB.",
                edit=("MAX_UPLOAD_BYTES = 25 * 1024 * 1024", "MAX_UPLOAD_BYTES = 75 * 1024 * 1024")),),
         near=("The upload limit error message now names the file.",
               "max upload size is also enforced by the proxy in front")),
    Fact("s11", "static", "Where does the webhook signing secret come from?",
         ("webhook secret", "webhook signing", "signing secret"),
         (Plant(0.83, "WEBHOOK_HMAC_KEY",
                assistant="The webhook signing secret is read from WEBHOOK_HMAC_KEY in the environment; it is "
                          "never written to the config file."),),
         near=("webhook signing uses HMAC-SHA256 over the raw body",
               "Rotated the test webhook secret in the fixture.")),
    Fact("s12", "static", "When do the nightly backups run?",
         ("nightly backup", "backup", "cron"),
         (Plant(0.16, "30 2 * * * /opt/bin/backup.sh --nightly",
                assistant="Nightly backups run at 02:30 from cron.",
                tool_result="30 2 * * * /opt/bin/backup.sh --nightly"),),
         near=("The backup job writes to the cold-storage bucket.",
               "cron output goes to the journal, not to mail")),
    Fact("s13", "static", "Who owns the payments service?",
         ("payments service", "owns the payments", "owner"),
         (Plant(0.62, "Priya Natarajan",
                user="Ping Priya Natarajan about this before merging, she owns the payments service."),),
         near=("The payments service exposes /healthz on the admin port.",
               "set the owner field in CODEOWNERS for the new directory")),
    Fact("s14", "static", "What does error E4127 mean?",
         ("E4127", "shard map"),
         (Plant(0.90, "shard map stale (generation 812 < 815)",
                assistant="E4127 means the shard map is stale; the router refreshes it on the next tick.",
                tool_result="ERROR E4127: shard map stale (generation 812 < 815)"),),
         near=("The shard map is loaded from the coordinator at boot.",)),
    Fact("s15", "static", "What is the minimum Python version we support?",
         ("requires-python", "minimum Python", "Python version"),
         (Plant(0.04, 'requires-python = ">=3.11"',
                user="We still support 3.11 as the minimum, don't use the 3.12 typing syntax.",
                tool_result='requires-python = ">=3.11"'),),
         near=("The Python version in the CI matrix comes from the tox config.",
               "pinned the Python version in .python-version for local runs")),
    Fact("s16", "static", "Which commit introduced the regression?",
         ("first bad commit", "regression", "bisect"),
         (Plant(0.47, "9c41e7d2 is the first bad commit",
                assistant="The regression came in with 9c41e7d2, the cache key change.",
                tool_result="9c41e7d2 is the first bad commit"),),
         near=("Added a regression test for the empty-batch case.",
               "git bisect needs a clean tree, so stashed the scratch files first",
               "no regression in the benchmark after the change")),
    Fact("s17", "static", "Which channel do incidents go to?",
         ("on-call", "oncall", "incident channel"),
         (Plant(0.33, "#inc-ledger-oncall",
                user="If the canary fails, post in #inc-ledger-oncall and page whoever is on-call."),),
         near=("The on-call runbook links the dashboards.",
               "paging rules live in oncall/escalation.yaml")),
    Fact("s18", "static", "What CIDR block does the VPC use?",
         ("cidr_block", "VPC CIDR", "CIDR"),
         (Plant(0.79, 'cidr_block = "10.42.0.0/16"', tool_result='  cidr_block = "10.42.0.0/16"',
                assistant="The VPC is 10.42.0.0/16, so the peering range cannot overlap with 10.42.x."),),
         near=("subnet CIDR ranges are computed with cidrsubnet()",
               'cidr_block = var.subnet_cidr')),
    Fact("r01", "rename", "What did we rename parse_header to?",
         ("parse_header", "renamed parse_header", "rename"),
         (Plant(0.30, "Renamed parse_header to decode_frame_header",
                assistant="Renamed parse_header to decode_frame_header in src/wire/frames.py and updated the "
                          "14 call sites.",
                edit=("def parse_header(buf: bytes) -> Header:", "def decode_frame_header(buf: bytes) -> Header:")),),
         near=("Would rename this helper but it is part of the public API.",),
         rename=("parse_header", "decode_frame_header")),
    Fact("r02", "rename", "What is UserCache called now?",
         ("UserCache", "renamed UserCache"),
         (Plant(0.55, "UserCache is now SessionCache",
                assistant="UserCache is now SessionCache (src/store/cache.py); the old name is gone from the "
                          "tree.",
                edit=("class UserCache(BaseCache):", "class SessionCache(BaseCache):")),),
         rename=("UserCache", "SessionCache")),
    Fact("r03", "rename", "What did fetch_rows become?",
         ("fetch_rows", "renamed fetch_rows"),
         (Plant(0.80, "replaced fetch_rows with stream_rows",
                assistant="I replaced fetch_rows with stream_rows so the export no longer loads the whole "
                          "table.",
                edit=("def fetch_rows(self, query):", "def stream_rows(self, query):")),),
         rename=("fetch_rows", "stream_rows")),
    Fact("r04", "rename", "What is the retry_backoff_ms setting called now?",
         ("retry_backoff_ms", "retry_backoff"),
         (Plant(0.15, "retry_backoff_ms is renamed to retry_base_delay_ms",
                user="Heads up: retry_backoff_ms is renamed to retry_base_delay_ms in the config schema, keep "
                     "reading the old key for one release."),),
         rename=("retry_backoff_ms", "retry_base_delay_ms")),
    Fact("c01", "changing", "What is the HTTP client timeout now?",
         ("client timeout", "HTTP_TIMEOUT", "timeout"),
         (Plant(0.10, "HTTP client timeout to 30 seconds", assistant="Set the HTTP client timeout to 30 seconds.",
                edit=("HTTP_TIMEOUT_S = 10", "HTTP_TIMEOUT_S = 30")),
          Plant(0.45, "HTTP client timeout to 45 seconds", assistant="Set the HTTP client timeout to 45 seconds.",
                edit=("HTTP_TIMEOUT_S = 30", "HTTP_TIMEOUT_S = 45")),
          Plant(0.85, "HTTP client timeout to 20 seconds", assistant="Set the HTTP client timeout to 20 seconds.",
                edit=("HTTP_TIMEOUT_S = 45", "HTTP_TIMEOUT_S = 20"))),
         near=("the read timeout is separate from the connect timeout",
               "pytest-timeout kills the test after 60 seconds",
               "a client timeout here surfaces as a 504 at the gateway")),
    Fact("c02", "changing", "What is the worker pool size?",
         ("worker pool", "pool size", "workers"),
         (Plant(0.05, "worker pool size is now 8", assistant="The worker pool size is now 8."),
          Plant(0.40, "worker pool size is now 16", assistant="The worker pool size is now 16."),
          Plant(0.70, "worker pool size is now 12", assistant="The worker pool size is now 12.")),
         near=("The worker pool drains before shutdown.",
               "connection pool size comes from the DSN",
               "two workers were idle for the whole run")),
    Fact("c03", "changing", "Which upstream API version is pinned?",
         ("API version", "pinned the upstream", "api_version"),
         (Plant(0.20, "upstream API version to 2024-06-01",
                assistant="Pinned the upstream API version to 2024-06-01."),
          Plant(0.50, "upstream API version to 2025-01-15",
                assistant="Pinned the upstream API version to 2025-01-15."),
          Plant(0.90, "upstream API version to 2025-09-30",
                assistant="Pinned the upstream API version to 2025-09-30.")),
         near=("the API version header is required on every request",
               "api_version is passed through from the client unchanged")),
    Fact("c04", "changing", "What is the ingest batch size?",
         ("batch size", "BATCH_SIZE", "ingest batch"),
         (Plant(0.12, "Ingest batch size set to 500", assistant="Ingest batch size set to 500.",
                edit=("BATCH_SIZE = 100", "BATCH_SIZE = 500")),
          Plant(0.33, "Ingest batch size set to 2000", assistant="Ingest batch size set to 2000.",
                edit=("BATCH_SIZE = 500", "BATCH_SIZE = 2000")),
          Plant(0.66, "Ingest batch size set to 1200", assistant="Ingest batch size set to 1200.",
                edit=("BATCH_SIZE = 2000", "BATCH_SIZE = 1200"))),
         near=("the batch size does not change the output order",
               "an ingest batch that fails is retried as a whole")),
    Fact("c05", "changing", "Which region do we deploy to?",
         ("deploy region", "region", "DEPLOY_REGION"),
         (Plant(0.25, "DEPLOY_REGION=us-east-1", assistant="Deploy region is us-east-1 for now.",
                tool_result="DEPLOY_REGION=us-east-1"),
          Plant(0.60, "DEPLOY_REGION=eu-west-1", assistant="Moved the deploy region to eu-west-1.",
                tool_result="DEPLOY_REGION=eu-west-1"),
          Plant(0.95, "DEPLOY_REGION=ap-southeast-2", assistant="Moved the deploy region to ap-southeast-2.",
                tool_result="DEPLOY_REGION=ap-southeast-2")),
         near=("the region is read from the instance metadata when unset",
               "region failover is manual for now")),
    Fact("c06", "changing", "What is the root log level?",
         ("log level", "LOG_LEVEL", "logging level"),
         (Plant(0.08, "Root log level set to INFO", assistant="Root log level set to INFO.",
                tool_result="LOG_LEVEL=INFO"),
          Plant(0.52, "Root log level set to DEBUG", assistant="Root log level set to DEBUG.",
                tool_result="LOG_LEVEL=DEBUG"),
          Plant(0.78, "Root log level set to WARNING", assistant="Root log level set to WARNING.",
                tool_result="LOG_LEVEL=WARNING")),
         near=("the log level of the third-party clients stays at WARNING",
               "LOG_LEVEL is ignored when --quiet is passed")),
    Fact("c07", "changing", "What is the cache TTL?",
         ("cache TTL", "TTL", "ttl_seconds"),
         (Plant(0.18, "cache TTL is 300 seconds", assistant="The cache TTL is 300 seconds.",
                edit=("ttl_seconds: int = 60", "ttl_seconds: int = 300")),
          Plant(0.48, "cache TTL is 900 seconds", assistant="The cache TTL is 900 seconds.",
                edit=("ttl_seconds: int = 300", "ttl_seconds: int = 900")),
          Plant(0.74, "cache TTL is 120 seconds", assistant="The cache TTL is 120 seconds.",
                edit=("ttl_seconds: int = 900", "ttl_seconds: int = 120"))),
         near=("the DNS TTL is outside our control",
               "a negative cache TTL disables caching")),
    Fact("c08", "changing", "What is max retries set to?",
         ("max retries", "MAX_RETRIES", "retries"),
         (Plant(0.22, "max retries is 3 for the uploader", assistant="max retries is 3 for the uploader.",
                edit=("MAX_RETRIES = 1", "MAX_RETRIES = 3")),
          Plant(0.57, "max retries is 5 for the uploader", assistant="max retries is 5 for the uploader.",
                edit=("MAX_RETRIES = 3", "MAX_RETRIES = 5")),
          Plant(0.88, "max retries is 4 for the uploader", assistant="max retries is 4 for the uploader.",
                edit=("MAX_RETRIES = 5", "MAX_RETRIES = 4"))),
         near=("retries are logged at DEBUG",
               "the max retries setting does not apply to 4xx responses")),
)

# Queries for the latency table (fixed): rare terms are on one page, common terms on nearly every
# page, multi-word queries on a few.
LATENCY_QUERIES: dict[str, tuple[str, ...]] = {
    "rare": ("E4127", "SUP-48213", "ctx-artifacts-euw3-7f2a"),
    "common": ("self", "return", "import"),
    "multi": ("the staging DB port", "failed with exit code", "Renamed parse_header to"),
}


def planted_turn(at: float, n_pages: int) -> int:
    """The turn (0-based page position) a plant at fraction ``at`` lands on in an ``n_pages`` session."""
    if n_pages <= 1:
        return 0
    return min(n_pages - 1, max(0, round(at * (n_pages - 1))))


def all_queries() -> list[str]:
    seen: list[str] = []
    for fact in FACTS:
        for q in fact.queries:
            if q not in seen:
                seen.append(q)
    return seen
