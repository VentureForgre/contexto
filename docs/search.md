# How search matches

`contexto_search` (the MCP tool the model calls) and `contexto search` (the command you run) use the
same search over a session's paged-out turns. This page says what a query matches, what happens when
nothing matches every word, and what search cannot do.

Search reads words only. It uses no embeddings and makes no model call, and the same store and query
always give the same result.

## Strict matching comes first

A query is split on spaces into words. A turn matches when it holds every word, in any case and any
order. A word matches as a substring, so identifiers, paths and parts of them work: `TIMEOUT` finds
`HTTP_TIMEOUT_S`.

```
query: staging database port

[best matching pages by relevance, shown newest first:]
pg_d3a8859d41f0620c (turn 3)
  38: [turn 3] assistant:
> 58: The staging database listens on port 6543 and production on 5432.
```

Lines marked `>` are the matching lines. The number before each line is its offset in the turn, which
`contexto_recall` (or `contexto recall PAGE_ID --offset N`) reads on from.
[mcp-tools.md](mcp-tools.md) describes ranking and the rest of the reply.

If any paged-out turn holds every word, or a line of the conversation still in view does, this is
all search does. The steps below never change such a result.

## When no turn holds every word

Search then looks for turns that hold part of the query, and the reply says so on its first line:

```
[no paged-out turn holds every word of that query; approximate matches, most words matched first, then newest first:]
```

Under each page a line names the words the page matched and the ones it did not. Four steps make a
match approximate. A query can use several of them at once.

### 1. Stop words and end punctuation are dropped

Question and function words are dropped from the query, and so is punctuation at the ends of a word
(`"`, `'`, `` ` ``, `(`, `[` in front; `"`, `'`, `` ` ``, `)`, `]`, `?`, `!`, `.`, `,`, `;`, `:`
behind).

```
query: what port is the staging database on?

[no paged-out turn holds every word of that query; approximate matches, most words matched first, then newest first:]
[searched for: port, staging, database]
pg_d3a8859d41f0620c (turn 3)
  [matched: port, staging, database]
  38: [turn 3] assistant:
> 58: The staging database listens on port 6543 and production on 5432.
```

The stop words are these 89:

> a about am an and are aren't as at be been but by can can't could did didn't do does doesn't don't
> for from had has have he here how how's i if in into is isn't it it's its may me might must my of on
> or our please shall she should tell than that that's the their then there there's these they this
> those to us was wasn't we were weren't what what's when where where's which who who's whom whose why
> will with would you your

A word with a digit or a symbol in it is never dropped. Words that mean something in code or logs,
such as `not`, `no`, `all`, `set`, `get`, `new`, `self`, `return` and `import`, are not on the list.
A query made only of stop words has no approximate matches.

At most 6 words are searched. A longer query keeps its 6 longest words, and the `[searched for: ...]`
line shows which.

### 2. Identifier styles match each other

A word written as an identifier is compared without `_`, `-` and `.`, in the query and in the turn.
So `refresh_token`, `refreshToken`, `refresh-token` and `refresh.token` all find each other.

```
query: refresh_token

pg_fda2a23e71e3ce0e (turn 1)
  [matched: refresh_token as refreshToken]
  35: [turn 1] assistant:
> 55: The login flow stores the refreshToken in the keychain and rotates it monthly.
```

A word counts as an identifier when it has `_`, `-` or `.` inside it, or a capital after its first
letter next to small letters (`refreshToken`, `HTTPTimeout`), and has at least 4 other characters,
one of them a letter, and no separator stands between two digits. A plain word, a number, a version
and a date are matched as typed, so `12` does not find `1.2`.

### 3. A turn need not hold every word

Turns are ranked by the words they hold. Turns that hold every searched word come first. After
them, a turn that holds the rarer words of the query ranks above one that holds the common ones.

```
query: staging database port zzqqxx

pg_d3a8859d41f0620c (turn 3)
  [matched: staging, database, port; not matched: zzqqxx]
  38: [turn 3] assistant:
> 58: The staging database listens on port 6543 and production on 5432.
```

Two facts on two turns are both found:

```
query: staging port deploys tuesday

pg_9ba3dd4bc3c37b6e (turn 4)
  [matched: deploys, tuesday; not matched: staging, port]
  31: [turn 4] assistant:
> 51: Deploys go out on Tuesday after the freeze ends.
pg_d3a8859d41f0620c (turn 3)
  [matched: staging, port; not matched: deploys, tuesday]
  38: [turn 3] assistant:
> 58: The staging database listens on port 6543 and production on 5432.
```

A floor keeps noise out. Each word has a weight: the rarer it is in the session, the more it weighs.

- A turn must hold at least 30 percent of the weight of the query's words that the session holds
  somewhere. In a 40-turn session where every turn says `build` and four turns each hold one of
  four rare words, a turn that holds only `build` holds 0.1 percent of a five-word query and is not
  returned.
- When some word of the query is in no turn at all, a turn must also hold more words than the number
  of those. `nightly backups zzqqxx` finds the turn about nightly backups (2 words held, 1 unknown).
  `nightly zzqqxx` finds nothing (1 held, 1 unknown).
- In that case the words the turn holds must also not be on more than half the turns together. If
  20 of 21 turns say both `return` and `value`, then `return value foo` finds nothing, unless a turn
  says `return value` in that order and most do not.
- One exception: a turn that holds an identifier, a number or a path from the query is returned even
  when the rest of the query is unknown, if at most half the turns hold that word. `What value did
  retry_budget have earlier?` finds the turn that sets `retry_budget`.

"Half the turns" applies from 10 turns up.

### 4. One typo

If the query has a plain word that no turn holds, search looks for a whole word one letter away
from it: one letter changed, added, left out, or two neighbours swapped.

```
query: nightly bakcups

pg_5b385fa4257da5fb (turn 0)
  [matched: nightly, bakcups as backups]
  2491: step 39: ran the build and checked the output, nothing unusual
> 2554: Nightly backups run at 02:00 from the replica.
```

The word must have 6 to 40 letters and be a plain word: no digit, no symbol, not an identifier. A
number is never corrected, so `SUP-48231` does not find `SUP-48213`. Only one word per query is
tried, the longest one that no turn holds. A word matched this way weighs half.

## What "approximate" means for the reader

An approximate match is a lead. The line under the page says which words were found and in what
form, and which were not, so you can tell a page that holds everything from a page that shares one
identifier with the query. When the reply lists `not matched` words, the session has no turn that
holds them together with the rest.

The reply shows at most 3 turns in detail and lists at most 10 more, each with its words after the
offsets:

```
[more approximate matches, recall one at its offset:]
pg_fca3845691490657 (turn 76) @46 [matched: deploy, finished; not matched: zzqqxx]
```

`contexto search` prints the same matches. A note on stderr (the second line below) says they are
approximate, and the words line follows each hit's index line:

```
$ contexto search refresh_token zzqqxx keychain
session s1 (most recent)
no paged-out turn in session s1 contains every word of 'refresh_token zzqqxx keychain'; approximate matches, best first:
pg_0767842a6270d008 turn 1: how does login work
  [matched: refresh_token as refreshToken, keychain; not matched: zzqqxx]
    35: [turn 1] assistant:
  > 55: The login flow stores the refreshToken in the keychain and rotates it monthly.
```

When nothing passes the floor, the reply is `No paged-out turn contains every word of that query.`
as before, and the command prints its usual "no paged-out turn" line. The exit code is 0 in every
case.

## Limits

- No synonyms and no stemming. `overnight` does not find `nightly`, and `restoring` does not find
  `restore`. (A typo one letter away does: `backups` finds `backup` when no turn says `backups`.)
- A plain lower-case word is not matched against a separated form. `retrybudget` does not find
  `retry_budget`; `retryBudget` and `retry-budget` do.
- An identifier split by spaces is not joined: `rate-limit` does not find `rate limit`.
- A typo is found only in a plain word, only as a whole word, and only one per query. `databse`
  finds `database` but not `databases`. A typo in an identifier (`refresh_tokne`) is not found.
- Typos are for ASCII letters. A word with an accent is matched as typed.
- In a session of thousands of turns, a typo is not searched when the halves of the word are very
  common (search stops after checking 500,000 places and treats the word as not found).
- The conversation still in view is matched strictly only: a line there counts when it holds every
  word.
- Falling back costs one more pass over the session's turns. On 6,000 synthetic turns of about
  32 KB each, a one-word query that finds nothing took 0.46 CPU seconds for the strict search and
  0.88 more for the fallback, 0.42 of that for the typo try; a seven-word question took 0.57 and
  1.68 more. A query that matches strictly costs what it did before.

The exact rules are in the design contract,
[docs/milestones/M2-contexto-proxy.md](milestones/M2-contexto-proxy.md), under `contexto_search`,
and in the docstrings of `src/contexto/store/search.py`.
