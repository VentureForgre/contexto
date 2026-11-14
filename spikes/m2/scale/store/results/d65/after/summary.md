# D65 probes: after

- Quality: commit 045cd599, 6,000 pages, 192 probes (set 38366a406bd3006a); Python 3.13.14, macOS 27.2.1, Apple M5 Pro; the store has `search_approximate`.
- Latency: commit 045cd599, 6,000 pages, 192 probes (set 38366a406bd3006a), 4 passes of 5 calls per query, 34 queries timed at 6,000 pages, baseline commit ef15af03; Python 3.13.14, macOS 27.2.1, Apple M5 Pro; the store has `search_approximate`.

## Recall at 6,000 pages

A probe counts when every expected page is in the top 3 (top 10) of the ranked pages. Reachable: strict search ranks the page in the top 3 (top 10) when the query is spelled the page's way (for a question, without its question mark); `-` for a class with no such spelling. Latest first: of the probes on a fact whose value changes, those whose expected page ranks above every older value's page.

| class | half | probes | top 3 | reachable at 3 | top 10 | reachable at 10 | latest first | strict / fallback / nothing |
|---|---|---:|---:|---:|---:|---:|---|---|
| exact | tune | 8 | 8 | 8 | 8 | 8 | 1 of 1 | 8 / 0 / 0 |
| exact | held-out | 7 | 7 | 7 | 7 | 7 | 2 of 2 | 7 / 0 / 0 |
| exact | all | 15 | 15 (100%) | 15 | 15 (100%) | 15 | 3 of 3 | 15 / 0 / 0 |
| identifier | tune | 11 | 8 | 8 | 9 | 9 | 2 of 2 | 0 / 11 / 0 |
| identifier | held-out | 12 | 12 | 12 | 12 | 12 | 3 of 3 | 0 / 12 / 0 |
| identifier | all | 23 | 20 (87%) | 20 | 21 (91%) | 21 | 5 of 5 | 0 / 23 / 0 |
| paraphrase | tune | 21 | 19 | - | 19 | - | 3 of 4 | 2 / 19 / 0 |
| paraphrase | held-out | 21 | 15 | - | 15 | - | 5 of 7 | 4 / 16 / 1 |
| paraphrase | all | 42 | 34 (81%) | - | 34 (81%) | - | 8 of 11 | 6 / 35 / 1 |
| question | tune | 15 | 13 | 2 | 13 | 2 | 3 of 3 | 0 / 15 / 0 |
| question | held-out | 15 | 13 | 1 | 13 | 1 | 4 of 5 | 0 / 14 / 1 |
| question | all | 30 | 26 (87%) | 3 | 26 (87%) | 3 | 7 of 8 | 0 / 29 / 1 |
| typo | tune | 15 | 11 | 15 | 11 | 15 | 0 of 1 | 0 / 11 / 4 |
| typo | held-out | 15 | 11 | 15 | 11 | 15 | 5 of 5 | 0 / 11 / 4 |
| typo | all | 30 | 22 (73%) | 30 | 22 (73%) | 30 | 5 of 6 | 0 / 22 / 8 |
| extra_word | tune | 13 | 12 | 13 | 12 | 13 | 1 of 1 | 1 / 12 / 0 |
| extra_word | held-out | 13 | 12 | 13 | 12 | 13 | 4 of 5 | 1 / 12 / 0 |
| extra_word | all | 26 | 24 (92%) | 26 | 24 (92%) | 26 | 5 of 6 | 2 / 24 / 0 |
| two_page | tune | 5 | 5 | - | 5 | - | 1 of 1 | 0 / 5 / 0 |
| two_page | held-out | 5 | 3 | - | 4 | - | 1 of 2 | 0 / 5 / 0 |
| two_page | all | 10 | 8 (80%) | - | 9 (90%) | - | 2 of 3 | 0 / 10 / 0 |
| two_page, either page | tune | 5 | 5 | - | 5 | - | - | 0 / 5 / 0 |
| two_page, either page | held-out | 5 | 5 | - | 5 | - | - | 0 / 5 / 0 |
| two_page, either page | all | 10 | 10 (100%) | - | 10 (100%) | - | - | 0 / 10 / 0 |

## Groups at 6,000 pages

The same counts for parts of a class, both halves together. The off-page word is the typo, the added word, or for a paraphrase the word its page does not hold (the most common one when there are several): on no page of the store, or on some.

| group | probes | top 3 | reachable at 3 | top 10 | reachable at 10 | latest first | strict / fallback / nothing |
|---|---:|---:|---:|---:|---:|---|---|
| identifier, the identifier alone | 20 | 17 (85%) | 17 | 18 (90%) | 18 | 4 of 4 | 0 / 20 / 0 |
| identifier, with other page words | 3 | 3 (100%) | 3 | 3 (100%) | 3 | 1 of 1 | 0 / 3 / 0 |
| typo, substitution | 9 | 8 (89%) | 9 | 8 (89%) | 9 | 2 of 2 | 0 / 8 / 1 |
| typo, deletion | 7 | 5 (71%) | 7 | 5 (71%) | 7 | 1 of 2 | 0 / 5 / 2 |
| typo, insertion | 5 | 3 (60%) | 5 | 3 (60%) | 5 | - | 0 / 3 / 2 |
| typo, swap | 9 | 6 (67%) | 9 | 6 (67%) | 9 | 2 of 2 | 0 / 6 / 3 |
| typo, the other words are the page's own | 15 | 14 (93%) | 15 | 14 (93%) | 15 | 2 of 3 | 0 / 14 / 1 |
| typo, the misspelled word carries the query | 15 | 8 (53%) | 15 | 8 (53%) | 15 | 3 of 3 | 0 / 8 / 7 |
| typo, the misspelled word carries the query, without identifier typos | 9 | 8 (89%) | 9 | 8 (89%) | 9 | 3 of 3 | 0 / 8 / 1 |
| typo, the misspelled word carries the query, identifier typos only | 6 | 0 (0%) | 6 | 0 (0%) | 6 | - | 0 / 0 / 6 |
| typo, typed word under 6 characters | 1 | 0 (0%) | 1 | 0 (0%) | 1 | 0 of 1 | 0 / 0 / 1 |
| extra_word, off-page word on no page | 12 | 12 (100%) | 12 | 12 (100%) | 12 | 2 of 2 | 0 / 12 / 0 |
| extra_word, off-page word on some page | 14 | 12 (86%) | 14 | 12 (86%) | 14 | 3 of 4 | 2 / 12 / 0 |
| paraphrase, off-page word on no page | 22 | 21 (95%) | - | 21 (95%) | - | 5 of 6 | 0 / 21 / 1 |
| paraphrase, off-page word on some page | 20 | 13 (65%) | - | 13 (65%) | - | 3 of 5 | 6 / 14 / 0 |

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
| identifier | 23 | 20 | 21 | 2 | 0 |
| paraphrase | 42 | 34 | 34 | 8 | 0 |
| question | 30 | 26 | 26 | 4 | 0 |
| typo | 30 | 22 | 22 | 8 | 0 |
| extra_word | 26 | 24 | 24 | 2 | 0 |
| two_page | 10 | 8 | 9 | 1 | 0 |

## Probe checks at 6,000 pages

- exact probes whose page strict search returns: 15 of 15.
- typo, identifier, extra_word and two_page probes for which strict search returns an expected page: 0 of 89 (0 is right).
- none probes with an absent word on some page: 0 of 16 (0 is right).
- paraphrase probes whose expected page holds every word of the query: 0 of 42 (0 is right).
- paraphrase and question probes that strict search answers, so a fallback never runs: 6 of 72; the expected page is among the pages returned for 0 of them.
- question probes strict search finds once the question mark is removed: 3 at rank 1, 3 in the top 3, 3 in the top 10, of 30.
- probes the fallback answered with a page id more than once: 0 of 143 (0 is right).
- probes the fallback answered with a page id that is not in the store: 0 of 143 (0 is right). Such an id still counts as a page returned, and as a false hit for `none`.

## Latency at 6,000 pages

Seconds through an MCP child over stdio: the median of the calls per query per pass, then the median over queries and passes. `miss_*` are queries strict search finds nothing for; `miss_typo_carried`, `miss_extra_word_common` and `miss_paraphrase_common` are fixed probes whose shape costs a fallback most. By pass: the lowest and the highest ratio of one pass alone.

| kind | queries | tree, s | baseline, s | tree / baseline | tree / baseline by pass, min to max |
|---|---:|---:|---:|---:|---:|
| rare | 3 | 0.280 | 0.280 | 1.00 | 0.99 to 1.03 |
| common | 3 | 0.258 | 0.254 | 1.01 | 0.99 to 1.05 |
| multi | 3 | 0.416 | 0.413 | 1.01 | 1.00 to 1.01 |
| miss_identifier | 3 | 0.625 | 0.259 | 2.41 | 2.37 to 2.44 |
| miss_question | 3 | 1.057 | 0.364 | 2.91 | 2.77 to 2.89 |
| miss_typo | 3 | 1.370 | 0.430 | 3.18 | 2.91 to 3.16 |
| miss_extra_word | 3 | 1.426 | 0.524 | 2.72 | 2.68 to 2.69 |
| miss_none | 3 | 1.219 | 0.444 | 2.74 | 2.86 to 2.96 |
| miss_paraphrase | 3 | 1.277 | 0.369 | 3.47 | 3.38 to 3.72 |
| miss_typo_carried | 3 | 0.628 | 0.294 | 2.13 | 2.24 to 2.42 |
| miss_extra_word_common | 2 | 1.273 | 0.585 | 2.17 | 2.26 to 2.29 |
| miss_paraphrase_common | 2 | 1.127 | 0.487 | 2.32 | 2.25 to 2.30 |

1-minute load average at the start of each pass: 11.72, 13.16, 8.66, 4.69.
4 passes: the tree went first in passes 1 and 3, the baseline in passes 2 and 4.
Peak RSS of the MCP child, MB: tree 140, baseline 138.

## First calls of a process at 6,000 pages

Each pass starts a new child per side, so its first search, its first strict miss and its first call of each `miss_*` kind are one-time costs, which the medians above hide. A cost a child pays once, on any stage of a miss, shows in the row of the first kind whose first query runs that stage. The first strict miss is also the first call of the first `miss_*` kind timed. The two sides are compared within a pass, under the same load. The side that goes first alternates by pass, so each odd pass is paired with the next even pass by geometric mean, which cancels the position. Paired: the geometric mean of those pairs. These rows rest on one call per side and pass, and are noisier than the rows above.

| call | query | tree, s | baseline, s | tree / baseline, paired | each pair |
|---|---|---:|---:|---:|---|
| first search | E4127 | 1.865 | 1.851 | 0.99 | passes 1-2: 0.97; passes 3-4: 1.01 |
| first strict miss | streamChunkedV3 | 0.604 | 0.250 | 2.42 | passes 1-2: 2.43; passes 3-4: 2.40 |
| first miss_identifier | streamChunkedV3 | 0.604 | 0.250 | 2.42 | passes 1-2: 2.43; passes 3-4: 2.40 |
| first miss_question | What port is the staging database on? | 0.763 | 0.343 | 2.21 | passes 1-2: 2.22; passes 3-4: 2.19 |
| first miss_typo | stagng DB port | 1.341 | 0.396 | 3.36 | passes 1-2: 3.30; passes 3-4: 3.42 |
| first miss_extra_word | staging DB port number | 1.538 | 0.503 | 3.07 | passes 1-2: 3.03; passes 3-4: 3.10 |
| first miss_none | kubernetes ingress annotations | 1.067 | 0.414 | 2.56 | passes 1-2: 2.54; passes 3-4: 2.57 |
| first miss_paraphrase | intermittently failing test | 1.311 | 0.339 | 3.83 | passes 1-2: 3.77; passes 3-4: 3.89 |
| first miss_typo_carried | allkeys-lru | 0.614 | 0.255 | 2.42 | passes 1-2: 2.43; passes 3-4: 2.40 |
| first miss_extra_word_common | webhook signing secret owner | 1.297 | 0.534 | 2.36 | passes 1-2: 2.32; passes 3-4: 2.41 |
| first miss_paraphrase_common | artifacts bucket name | 1.244 | 0.492 | 2.54 | passes 1-2: 2.53; passes 3-4: 2.54 |

Seconds are medians over passes. By pass:

| pass | load | went first | first search: tree, s | baseline, s | tree / baseline | first strict miss: tree, s | baseline, s | tree / baseline |
|---:|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | 11.72 | tree | 2.235 | 2.400 | 0.93 | 0.630 | 0.262 | 2.40 |
| 2 | 13.16 | baseline | 2.022 | 2.006 | 1.01 | 0.634 | 0.258 | 2.46 |
| 3 | 8.66 | tree | 1.709 | 1.686 | 1.01 | 0.557 | 0.230 | 2.42 |
| 4 | 4.69 | baseline | 1.705 | 1.696 | 1.01 | 0.578 | 0.243 | 2.38 |

The first call of each `miss_*` kind, tree / baseline by pass (the seconds are in `latency.csv`, rep 0 of the kind's first query):

| pass | went first | miss_identifier | miss_question | miss_typo | miss_extra_word | miss_none | miss_paraphrase | miss_typo_carried | miss_extra_word_common | miss_paraphrase_common |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | tree | 2.40 | 2.22 | 3.43 | 3.05 | 2.52 | 3.72 | 2.44 | 2.18 | 2.51 |
| 2 | baseline | 2.46 | 2.22 | 3.18 | 3.02 | 2.56 | 3.82 | 2.43 | 2.47 | 2.54 |
| 3 | tree | 2.42 | 2.23 | 3.43 | 3.09 | 2.59 | 3.92 | 2.38 | 2.39 | 2.57 |
| 4 | baseline | 2.38 | 2.16 | 3.41 | 3.12 | 2.55 | 3.86 | 2.42 | 2.43 | 2.52 |
