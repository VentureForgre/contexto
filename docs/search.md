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

Stop words are dropped from every query, not only from questions: `for loops` searches `loops`, and
`do while` searches `while`. The `[searched for: ...]` line shows what was searched whenever a word
was dropped.

At most 6 words are searched. A longer query keeps its identifiers, numbers and paths first, then
its longest words, and the `[searched for: ...]` line shows which. So a long question that ends in
`port 6543` still searches `6543`.

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
one of them a letter. A separator at the start or the end of a word does not count: `--force` and
`__init__` are matched as typed, so `--force` does not find `enforce`. A word typed twice counts as
an identifier when either spelling does: `refreshtoken refreshToken` and `refreshToken refreshtoken`
both find `refresh_token`.

A separator between two digits is never dropped, in the query or in the turn, because dropping it
makes another number. `12` does not find `1.2`, `port-80` does not find `port8.0`, and `sha.256`
finds `sha_256` but not `sha-2.56`. A plain word, a number, a version and a date are matched as
typed.

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
A word that no turn holds weighs the most, as much as a word can. A turn is returned when it holds
at least 30 percent of the weight of the query.

- In a 40-turn session where every turn says `build` and four turns each hold one of four rare
  words, a turn that holds only `build` holds 0.1 percent of a five-word query and is not returned.
- A word that no turn holds counts against every turn. In a 7-turn session, `nightly zzqqxx` finds
  the turn that says `nightly` (38 percent of the query) and names `zzqqxx` as not matched.
  `nightly zzqqxx yyqqxx` finds nothing (23 percent).
- A common word does not carry an unknown one. If 20 of 21 turns say `return` and `value`, then
  `return value foo` finds nothing.
- One exception: a turn that holds an identifier, a number or a path from the query is returned
  whatever its share, unless more than half the turns hold that word. `What value did retry_budget
  have earlier?` finds the turn that sets `retry_budget`. Such a word has 3 characters or more: `5`
  alone does not count.

When no turn holds every word, the query may ask for two facts that sit on two turns. The first
fact's turn then comes first, followed by turns that share words with it, and the second fact's
turn can fall far behind or under the floor. So the third of the 3 turns shown in detail goes to the
best turn for the words the first turn lacks. In a 40-turn test session, `staging gateway port redis
eviction policy` shows the Redis turn first and the staging turn third; without this place the
staging turn is fifth. In a 100-turn session where turns 20 to 22 hold `staging database port` and
turn 40 says `backups run at 02:00`, `staging database port bakcups` shows turn 40 third; without
this place it is not returned at all.

A turn takes this place when the lacked words it holds weigh at least 30 percent of all the lacked
words, and one of these is true (an identifier, a number or a path that is rare enough to pass the
second rule on its own needs no 30 percent):

- One of them is an identifier, a number or a path that half the turns or fewer hold.
- They weigh at least half as much as a word that no turn holds. A word matched through a typo
  counts at its full weight here, not at half.
- One of them stands next to its neighbour from the query, in the query's order, with one space
  between them on the turn. A stop word between them, in the query or on the turn, breaks the pair:
  for `restart the nginx`, a turn that says `restart nginx` holds no pair. In a 100-turn
  session where `rate` and `limit` are each on 30 turns, `webhook signing secret and API rate limit`
  shows the turn that says `the API rate limit is 600 a minute` third.

The 30 percent keeps out a common identifier or number when the turn lacks the rarer words. If 40
of 100 turns say `2024` and 10 say `retention`, then for `deploy pipeline 2024 retention` a turn that
holds only `2024` gets no place: it holds 0.91 of the lacked 3.18, under the 0.95 needed. A rare
identifier needs no such share. In a 1,000-turn session where turns 10 to 29 say `staging database
port` and turn 600 says `error e4127 came from shard seven`, `staging database port e4127 replica
cutover` shows turn 600 third, although `replica` and `cutover` are on no turn and outweigh it.

The second rule keeps out a leftover common word. One word passes it when it is on at most
`sqrt((n + 1) / 2) - 1/2` of `n` turns: 6 of 100 turns, and 54 of 6,000. So a word on more than
about 6.6 percent of 100 turns, or 0.9 percent of 6,000, gets no place unless it stands next to its
neighbour from the query. If 12 of 100 turns say `server`, `canary rollout plan server` shows no
turn for `server`; a turn that says `plan server` would pass.

Among the turns that pass, a turn wins over a paged context block, then the one that holds more of
the lacked words wins, then the one that holds more of the whole query, then the one with more of
the query's words side by side as typed, then the newer one. So for `how do I restart the nginx
proxy`, a turn that says `restart the nginx service` beats a newer turn that says only `restart the
laptop`. And when an old turn says `prod host is db1` and a newer one `prod host is now db2`, the
newer one takes the place, even if the old one says it in more places.

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

One letter left out or added also joins a word and its form with one more letter: `servers` finds
`server`, and `tokens` finds `token`, when no turn says the word as typed. This is the only place
where search matches a word to a different form of it.

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
approximate, followed by the `[searched for: ...]` line when words were dropped, and the words line
follows each hit's index line:

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

- No synonyms and no stemming. `overnight` does not find `nightly`, `restoring` does not find
  `restore`, and `database` does not find a turn that says `DB`. In the 6,000-turn test session,
  `What port is the staging database on?` put the turn that says "staging DB port" 26th, behind the
  turns that hold all three words.
- The typo step is the one exception to "no stemming", for one letter only: `servers` finds
  `server`. It applies only to a word that no turn holds as typed.
- Stop words are dropped from every query. `for loops` searches `loops` and `do while` searches
  `while`; `which`, `may`, `will`, `can` and `then` are dropped even where they are code or log
  words.
- A plain lower-case word is not matched against a separated form. `retrybudget` does not find
  `retry_budget`, and `nodejs` does not find `node.js`; `retryBudget`, `retry-budget` and `nodeJs`
  do.
- Folding sees only the letters. `doneNext` finds `done.Next` at the end of a sentence, and
  `a.b@example.com` finds `ab@example.com`.
- When one turn writes an identifier in several styles, the `>` lines show the first style found.
- An identifier split by spaces is not joined: `rate-limit` does not find `rate limit`.
- A typo is found only in a plain word, only as a whole word, and only one per query. `databse`
  finds `database` but not `databases`. A typo in an identifier (`refresh_tokne`) is not found.
- Typos are for ASCII letters. A word with an accent is matched as typed.
- In a session of thousands of turns, a typo is not searched when the halves of the word are very
  common (search stops after checking 500,000 places for the whole query and treats the word as not
  found).
- A second fact of one word is not returned when the word is on more than about 6.6 percent of 100
  turns, or 0.9 percent of 6,000, unless the turn has it next to its neighbour from the query. In a
  100-turn session, `redis eviction policy cutover` shows the newest turn that says `cutover` when
  6 turns say it, and no turn for it when 7 do.
- When no turn holds every word, the order can put an older value first. The rule that the newest
  turn holding every word is shown in detail holds only among turns that hold every word. If turns
  10 to 12 hold `staging database port`, turn 13 says `staging prod host is db1` and the newer
  turn 15 `prod host is now db2`, then `staging database port prod host` shows turn 13 first and
  puts turn 15 fifth, in the list.
- A common word takes the third place when any turn has it next to its query neighbour, even by
  accident. With `server` on 12 of 100 turns, a log line `listening port server api-1 started` takes
  the place for `staging database port server`, and the staging turn it displaces is listed fourth.
  The pair is matched as a substring, like any word: `an accurate limits table` holds `rate limit`.
- One identifier typed in two styles in one query counts as two words: `refresh_token refreshToken`
  weighs twice as much as one of them and takes two of the 6 words searched.
- Two facts on two turns are both shown when the second passes the rules for the third place.
  Four equally rare facts on four turns find nothing: each turn holds a quarter of the query, under
  the floor, so no turn comes first for the second-fact place to follow. `kestrel osprey harrier
  condor`, one word per turn, finds nothing. Three equally rare facts on three turns are all
  returned (a third each).
- Identifiers that differ only in the separator between two groups of digits do not match:
  `x86_64` does not find `x86-64`, and `build-2026-11-05` does not find `build_2026_11_05`.
- The exception for an identifier, a number or a path lets a common one through when it is on half
  the turns or fewer. If 34 of 100 turns say `200` and no turn holds `staging`, `deploy` or
  `failed`, then `staging deploy failed 200` returns those 34 turns.
- When the query has no unknown word, the floor does not look at how common the held words are.
  Three words that are each on 30 of 100 turns, never together, return 90 turns: 3 in detail, 10
  listed, and the rest counted.
- The conversation still in view is matched strictly only: a line there counts when it holds every
  word.
- Falling back costs one more pass over the session's turns. On 6,000 synthetic turns of about
  32 KB each, a query that finds nothing cost about 2 times what the strict search alone costs when
  no typo is tried, and about 3 times when one is (a one-word query: 0.48 CPU seconds strict, 0.91
  more for the fallback; a seven-word question with a typo: 0.35 and 0.87 more). Those turns were
  read from disk. When every turn is held in memory (a session whose search text fits in the 64 MB
  budget), a strict miss is cheaper, because it stops at a turn's first missing word, so the ratio
  is higher: 2.0 to 11 times on 1,000 synthetic turns on a busy machine. Those misses took at most 0.13 CPU seconds.
  A query that matches strictly costs what it did before.

The exact rules are in the design contract,
[docs/milestones/M2-contexto-proxy.md](milestones/M2-contexto-proxy.md), under `contexto_search`,
and in the docstrings of `src/contexto/store/search.py`.
