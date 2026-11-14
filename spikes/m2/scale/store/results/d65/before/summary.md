# D65 probes: before

- Quality: commit 698acaa2, 6,000 pages, 192 probes (set 38366a406bd3006a); Python 3.13.14, macOS 26.6.1, Apple M5 Pro; the store has no `search_approximate`.
- Latency: commit 01d6dfc0, 6,000 pages, 192 probes (set 38366a406bd3006a), 4 passes of 5 calls per query, 34 queries timed at 6,000 pages, baseline commit ef15af03; Python 3.13.14, macOS 27.2.1, Apple M5 Pro; the store has no `search_approximate`.
- The two parts were not measured on the same commit (quality 698acaa2, latency 01d6dfc0). Read them separately.

## Recall at 6,000 pages

A probe counts when every expected page is in the top 3 (top 10) of the ranked pages. Reachable: strict search ranks the page in the top 3 (top 10) when the query is spelled the page's way (for a question, without its question mark); `-` for a class with no such spelling. Latest first: of the probes on a fact whose value changes, those whose expected page ranks above every older value's page.

| class | half | probes | top 3 | reachable at 3 | top 10 | reachable at 10 | latest first | strict / fallback / nothing |
|---|---|---:|---:|---:|---:|---:|---|---|
| exact | tune | 8 | 8 | 8 | 8 | 8 | 1 of 1 | 8 / 0 / 0 |
| exact | held-out | 7 | 7 | 7 | 7 | 7 | 2 of 2 | 7 / 0 / 0 |
| exact | all | 15 | 15 (100%) | 15 | 15 (100%) | 15 | 3 of 3 | 15 / 0 / 0 |
| identifier | tune | 11 | 0 | 8 | 0 | 9 | 0 of 2 | 0 / 0 / 11 |
| identifier | held-out | 12 | 0 | 12 | 0 | 12 | 0 of 3 | 0 / 0 / 12 |
| identifier | all | 23 | 0 (0%) | 20 | 0 (0%) | 21 | 0 of 5 | 0 / 0 / 23 |
| paraphrase | tune | 21 | 0 | - | 0 | - | 0 of 4 | 2 / 0 / 19 |
| paraphrase | held-out | 21 | 0 | - | 0 | - | 0 of 7 | 4 / 0 / 17 |
| paraphrase | all | 42 | 0 (0%) | - | 0 (0%) | - | 0 of 11 | 6 / 0 / 36 |
| question | tune | 15 | 0 | 2 | 0 | 2 | 0 of 3 | 0 / 0 / 15 |
| question | held-out | 15 | 0 | 1 | 0 | 1 | 0 of 5 | 0 / 0 / 15 |
| question | all | 30 | 0 (0%) | 3 | 0 (0%) | 3 | 0 of 8 | 0 / 0 / 30 |
| typo | tune | 15 | 0 | 15 | 0 | 15 | 0 of 1 | 0 / 0 / 15 |
| typo | held-out | 15 | 0 | 15 | 0 | 15 | 0 of 5 | 0 / 0 / 15 |
| typo | all | 30 | 0 (0%) | 30 | 0 (0%) | 30 | 0 of 6 | 0 / 0 / 30 |
| extra_word | tune | 13 | 0 | 13 | 0 | 13 | 0 of 1 | 1 / 0 / 12 |
| extra_word | held-out | 13 | 0 | 13 | 0 | 13 | 0 of 5 | 1 / 0 / 12 |
| extra_word | all | 26 | 0 (0%) | 26 | 0 (0%) | 26 | 0 of 6 | 2 / 0 / 24 |
| two_page | tune | 5 | 0 | - | 0 | - | 0 of 1 | 0 / 0 / 5 |
| two_page | held-out | 5 | 0 | - | 0 | - | 0 of 2 | 0 / 0 / 5 |
| two_page | all | 10 | 0 (0%) | - | 0 (0%) | - | 0 of 3 | 0 / 0 / 10 |
| two_page, either page | tune | 5 | 0 | - | 0 | - | - | 0 / 0 / 5 |
| two_page, either page | held-out | 5 | 0 | - | 0 | - | - | 0 / 0 / 5 |
| two_page, either page | all | 10 | 0 (0%) | - | 0 (0%) | - | - | 0 / 0 / 10 |

## Groups at 6,000 pages

The same counts for parts of a class, both halves together. The off-page word is the typo, the added word, or for a paraphrase the word its page does not hold (the most common one when there are several): on no page of the store, or on some.

| group | probes | top 3 | reachable at 3 | top 10 | reachable at 10 | latest first | strict / fallback / nothing |
|---|---:|---:|---:|---:|---:|---|---|
| identifier, the identifier alone | 20 | 0 (0%) | 17 | 0 (0%) | 18 | 0 of 4 | 0 / 0 / 20 |
| identifier, with other page words | 3 | 0 (0%) | 3 | 0 (0%) | 3 | 0 of 1 | 0 / 0 / 3 |
| typo, substitution | 9 | 0 (0%) | 9 | 0 (0%) | 9 | 0 of 2 | 0 / 0 / 9 |
| typo, deletion | 7 | 0 (0%) | 7 | 0 (0%) | 7 | 0 of 2 | 0 / 0 / 7 |
| typo, insertion | 5 | 0 (0%) | 5 | 0 (0%) | 5 | - | 0 / 0 / 5 |
| typo, swap | 9 | 0 (0%) | 9 | 0 (0%) | 9 | 0 of 2 | 0 / 0 / 9 |
| typo, the other words are the page's own | 15 | 0 (0%) | 15 | 0 (0%) | 15 | 0 of 3 | 0 / 0 / 15 |
| typo, the misspelled word carries the query | 15 | 0 (0%) | 15 | 0 (0%) | 15 | 0 of 3 | 0 / 0 / 15 |
| typo, the misspelled word carries the query, without identifier typos | 9 | 0 (0%) | 9 | 0 (0%) | 9 | 0 of 3 | 0 / 0 / 9 |
| typo, the misspelled word carries the query, identifier typos only | 6 | 0 (0%) | 6 | 0 (0%) | 6 | - | 0 / 0 / 6 |
| typo, typed word under 6 characters | 1 | 0 (0%) | 1 | 0 (0%) | 1 | 0 of 1 | 0 / 0 / 1 |
| extra_word, off-page word on no page | 12 | 0 (0%) | 12 | 0 (0%) | 12 | 0 of 2 | 0 / 0 / 12 |
| extra_word, off-page word on some page | 14 | 0 (0%) | 14 | 0 (0%) | 14 | 0 of 4 | 2 / 0 / 12 |
| paraphrase, off-page word on no page | 22 | 0 (0%) | - | 0 (0%) | - | 0 of 6 | 0 / 0 / 22 |
| paraphrase, off-page word on some page | 20 | 0 (0%) | - | 0 (0%) | - | 0 of 5 | 6 / 0 / 14 |

## Probes strict search answers without the expected page at 6,000 pages

Some page holds every word of the query, so a fallback that runs only on an empty result never runs for these.

| probe | query | pages strict search returns |
|---|---|---:|
| paraphrase-01 | staging database port | 25 |
| paraphrase-31 | cache timeout seconds | 10 |
| paraphrase-32 | flaky test name | 11 |
| paraphrase-34 | fetch_rows rename | 11 |
| paraphrase-35 | UserCache rename | 7 |
| paraphrase-41 | pool size workers | 24 |
| extra_word-17 | public API rate limit timeout | 1 |
| extra_word-26 | HTTP client timeout retries | 1 |

## Nothing to find (`none`) at 6,000 pages

A false hit is a probe for which any page is returned.

| sub-kind | probes | false hits: tune | held-out | all | median pages returned | through the tool: probes with a page |
|---|---:|---:|---:|---:|---:|---:|
| absent | 8 | 0/4 | 0/4 | 0/8 (0%) | 0 | 0 |
| mixed | 8 | 0/4 | 0/4 | 0/8 (0%) | 0 | 0 |
| all | 16 | 0/8 | 0/8 | 0/16 (0%) | 0 | 0 |

## Through the tool (`contexto_search`) at 6,000 pages

Whether the reply shows every expected page in detail (the tool details 3 pages and lists 50).

| class | probes | in detail | in detail or listed | not in the reply | differs from top 3 above |
|---|---:|---:|---:|---:|---:|
| exact | 15 | 15 | 15 | 0 | 0 |
| identifier | 23 | 0 | 0 | 23 | 0 |
| paraphrase | 42 | 0 | 0 | 42 | 0 |
| question | 30 | 0 | 0 | 30 | 0 |
| typo | 30 | 0 | 0 | 30 | 0 |
| extra_word | 26 | 0 | 0 | 26 | 0 |
| two_page | 10 | 0 | 0 | 10 | 0 |

## Probe checks at 6,000 pages

- exact probes whose page strict search returns: 15 of 15.
- typo, identifier, extra_word and two_page probes for which strict search returns an expected page: 0 of 89 (0 is right).
- none probes with an absent word on some page: 0 of 16 (0 is right).
- paraphrase probes whose expected page holds every word of the query: 0 of 42 (0 is right).
- paraphrase and question probes that strict search answers, so a fallback never runs: 6 of 72; the expected page is among the pages returned for 0 of them.
- question probes strict search finds once the question mark is removed: 3 at rank 1, 3 in the top 3, 3 in the top 10, of 30.
- probes the fallback answered with a page id more than once: 0 of 0 (0 is right).
- probes the fallback answered with a page id that is not in the store: 0 of 0 (0 is right). Such an id still counts as a page returned, and as a false hit for `none`.

## Latency at 6,000 pages

Seconds through an MCP child over stdio: the median of the calls per query per pass, then the median over queries and passes. `miss_*` are queries strict search finds nothing for; `miss_typo_carried`, `miss_extra_word_common` and `miss_paraphrase_common` are fixed probes whose shape costs a fallback most. By pass: the lowest and the highest ratio of one pass alone.

| kind | queries | tree, s | baseline, s | tree / baseline | tree / baseline by pass, min to max |
|---|---:|---:|---:|---:|---:|
| rare | 3 | 0.284 | 0.289 | 0.98 | 0.98 to 1.00 |
| common | 3 | 0.272 | 0.268 | 1.01 | 0.97 to 1.02 |
| multi | 3 | 0.426 | 0.426 | 1.00 | 1.00 to 1.01 |
| miss_identifier | 3 | 0.271 | 0.270 | 1.00 | 0.99 to 1.02 |
| miss_question | 3 | 0.373 | 0.373 | 1.00 | 0.98 to 1.00 |
| miss_typo | 3 | 0.460 | 0.460 | 1.00 | 0.98 to 1.01 |
| miss_extra_word | 3 | 0.549 | 0.553 | 0.99 | 0.99 to 1.00 |
| miss_none | 3 | 0.463 | 0.455 | 1.02 | 0.91 to 1.00 |
| miss_paraphrase | 3 | 0.444 | 0.411 | 1.08 | 0.94 to 1.02 |
| miss_typo_carried | 3 | 0.340 | 0.333 | 1.02 | 0.96 to 1.04 |
| miss_extra_word_common | 2 | 0.610 | 0.609 | 1.00 | 1.00 to 1.01 |
| miss_paraphrase_common | 2 | 0.534 | 0.532 | 1.00 | 1.00 to 1.01 |

1-minute load average at the start of each pass: 8.95, 11.61, 10.9, 10.92.
4 passes: the tree went first in passes 1 and 3, the baseline in passes 2 and 4.
Peak RSS of the MCP child, MB: tree 139, baseline 141.

## First calls of a process at 6,000 pages

Each pass starts a new child per side, so its first search, its first strict miss and its first call of each `miss_*` kind are one-time costs, which the medians above hide. A cost a child pays once, on any stage of a miss, shows in the row of the first kind that runs that stage. The first strict miss is also the first call of the first `miss_*` kind timed. The two sides are compared within a pass, under the same load. The side that goes first alternates by pass, so each odd pass is paired with the next even pass by geometric mean, which cancels the position. Paired: the geometric mean of those pairs. These rows rest on one call per side and pass, and are noisier than the rows above.

| call | query | tree, s | baseline, s | tree / baseline, paired | each pair |
|---|---|---:|---:|---:|---|
| first search | E4127 | 2.060 | 2.081 | 1.01 | passes 1-2: 0.98; passes 3-4: 1.04 |
| first strict miss | streamChunkedV3 | 0.261 | 0.260 | 0.99 | passes 1-2: 0.99; passes 3-4: 0.99 |
| first miss_identifier | streamChunkedV3 | 0.261 | 0.260 | 0.99 | passes 1-2: 0.99; passes 3-4: 0.99 |
| first miss_question | What port is the staging database on? | 0.368 | 0.367 | 1.00 | passes 1-2: 1.01; passes 3-4: 0.98 |
| first miss_typo | stagng DB port | 0.417 | 0.423 | 0.99 | passes 1-2: 0.99; passes 3-4: 0.99 |
| first miss_extra_word | staging DB port number | 0.511 | 0.516 | 0.99 | passes 1-2: 0.99; passes 3-4: 0.99 |
| first miss_none | kubernetes ingress annotations | 0.448 | 0.449 | 1.00 | passes 1-2: 1.01; passes 3-4: 1.00 |
| first miss_paraphrase | intermittently failing test | 0.354 | 0.352 | 1.04 | passes 1-2: 1.00; passes 3-4: 1.09 |
| first miss_typo_carried | allkeys-lru | 0.295 | 0.304 | 0.97 | passes 1-2: 0.96; passes 3-4: 0.99 |
| first miss_extra_word_common | webhook signing secret owner | 0.575 | 0.575 | 1.00 | passes 1-2: 0.99; passes 3-4: 1.01 |
| first miss_paraphrase_common | artifacts bucket name | 0.535 | 0.537 | 1.00 | passes 1-2: 1.01; passes 3-4: 0.99 |

Seconds are medians over passes. By pass:

| pass | load | went first | first search: tree, s | baseline, s | tree / baseline | first strict miss: tree, s | baseline, s | tree / baseline |
|---:|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | 8.95 | tree | 2.028 | 2.078 | 0.98 | 0.252 | 0.251 | 1.00 |
| 2 | 11.61 | baseline | 1.924 | 1.941 | 0.99 | 0.271 | 0.275 | 0.98 |
| 3 | 10.9 | tree | 2.345 | 2.160 | 1.09 | 0.272 | 0.270 | 1.01 |
| 4 | 10.92 | baseline | 2.093 | 2.085 | 1.00 | 0.246 | 0.251 | 0.98 |

The first call of each `miss_*` kind, tree / baseline by pass (the seconds are in `latency.csv`, rep 0 of the kind's first query):

| pass | went first | miss_identifier | miss_question | miss_typo | miss_extra_word | miss_none | miss_paraphrase | miss_typo_carried | miss_extra_word_common | miss_paraphrase_common |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | tree | 1.00 | 1.02 | 0.98 | 0.98 | 1.03 | 0.99 | 0.93 | 0.97 | 1.01 |
| 2 | baseline | 0.98 | 1.01 | 1.00 | 1.00 | 0.99 | 1.02 | 0.99 | 1.01 | 1.00 |
| 3 | tree | 1.01 | 0.99 | 1.00 | 0.98 | 1.01 | 1.03 | 1.01 | 1.00 | 0.99 |
| 4 | baseline | 0.98 | 0.97 | 0.98 | 1.01 | 0.98 | 1.15 | 0.96 | 1.02 | 0.99 |
