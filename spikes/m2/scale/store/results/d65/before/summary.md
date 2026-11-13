# D65 probes: before

- Quality: commit 698acaa2, 6,000 pages, 192 probes (set 38366a406bd3006a); Python 3.13.14, macOS 26.6.1, Apple M5 Pro; the store has no `search_approximate`.
- Latency: commit 698acaa2, 6,000 pages, 192 probes (set 38366a406bd3006a), 4 passes of 5 calls per query, baseline commit 698acaa2; Python 3.13.14, macOS 26.6.1, Apple M5 Pro; the store has no `search_approximate`.

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
| typo, typed word under 6 characters | 1 | 0 (0%) | 1 | 0 (0%) | 1 | 0 of 1 | 0 / 0 / 1 |
| typo, off-page word on no page | 30 | 0 (0%) | 30 | 0 (0%) | 30 | 0 of 6 | 0 / 0 / 30 |
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

## Latency at 6,000 pages

Seconds through an MCP child over stdio: the median of the calls per query per pass, then the median over queries and passes. `first` is the first search of a process, after a throwaway child's search has filled the OS file cache, by whether that side searched first or second in its pass. `miss_*` are queries strict search finds nothing for.

| kind | queries | tree, s | baseline, s | tree / baseline |
|---|---:|---:|---:|---:|
| first (searched first) | 1 | 11.119 | 10.467 | 1.06 |
| first (searched second) | 1 | 9.321 | 8.864 | 1.05 |
| rare | 3 | 0.983 | 0.999 | 0.98 |
| common | 3 | 0.703 | 0.652 | 1.08 |
| multi | 3 | 1.410 | 1.331 | 1.06 |
| miss_identifier | 3 | 1.275 | 1.244 | 1.03 |
| miss_question | 3 | 1.658 | 1.596 | 1.04 |
| miss_typo | 3 | 2.082 | 2.203 | 0.94 |
| miss_extra_word | 3 | 1.050 | 1.180 | 0.89 |
| miss_none | 3 | 1.265 | 1.220 | 1.04 |

1-minute load average at the start of each pass: 42.21, 48.39, 66.91, 28.81.
Peak RSS of the MCP child, MB: tree 125, baseline 126.
