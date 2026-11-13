# D65 probes: before

Commit 835dadcf, Python 3.13.14, macOS 26.6.1, Apple M5 Pro. 154 probes; the store has no `search_approximate`.

## Recall at 6,000 pages

A probe counts when every expected page is in the top 3 (top 10) of the ranked pages.

| class | probes | top 3: tune | held-out | all | top 10: tune | held-out | all | answered by strict / fallback / nothing |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| exact | 15 | 8/8 | 7/7 | 15/15 (100%) | 8/8 | 7/7 | 15/15 (100%) | 15 / 0 / 0 |
| identifier | 23 | 0/12 | 0/11 | 0/23 (0%) | 0/12 | 0/11 | 0/23 (0%) | 0 / 0 / 23 |
| paraphrase | 30 | 0/15 | 0/15 | 0/30 (0%) | 0/15 | 0/15 | 0/30 (0%) | 1 / 0 / 29 |
| question | 30 | 0/15 | 0/15 | 0/30 (0%) | 0/15 | 0/15 | 0/30 (0%) | 0 / 0 / 30 |
| typo | 15 | 0/8 | 0/7 | 0/15 (0%) | 0/8 | 0/7 | 0/15 (0%) | 0 / 0 / 15 |
| extra_word | 15 | 0/8 | 0/7 | 0/15 (0%) | 0/8 | 0/7 | 0/15 (0%) | 0 / 0 / 15 |
| two_page | 10 | 0/5 | 0/5 | 0/10 (0%) | 0/5 | 0/5 | 0/10 (0%) | 0 / 0 / 10 |
| two_page, either page | 10 | 0/5 | 0/5 | 0/10 (0%) | 0/5 | 0/5 | 0/10 (0%) | |

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
| paraphrase | 30 | 0 | 0 | 30 | 0 |
| question | 30 | 0 | 0 | 30 | 0 |
| typo | 15 | 0 | 0 | 15 | 0 |
| extra_word | 15 | 0 | 0 | 15 | 0 |
| two_page | 10 | 0 | 0 | 10 | 0 |

## Probe checks at 6,000 pages

- exact probes whose page strict search returns: 15 of 15.
- typo, identifier, extra_word and two_page probes for which strict search returns an expected page: 0 of 63 (0 is right).
- none probes with an absent word on some page: 0 of 16 (0 is right).
- paraphrase and question probes that strict search answers, so a fallback never runs: 1 of 60; the expected page is among the pages returned for 0 of them.

## Latency at 6,000 pages

Seconds through an MCP child over stdio: the median of the calls per query per pass, then the median over queries and passes. `first` is the first search of a process. `miss_*` are queries strict search finds nothing for.

| kind | queries | tree, s | baseline, s | tree / baseline |
|---|---:|---:|---:|---:|
| first | 1 | 10.983 | 9.391 | 1.17 |
| rare | 3 | 0.755 | 0.661 | 1.14 |
| common | 3 | 0.608 | 0.590 | 1.03 |
| multi | 3 | 0.886 | 0.869 | 1.02 |
| miss_identifier | 3 | 0.613 | 0.650 | 0.94 |
| miss_question | 3 | 1.096 | 0.985 | 1.11 |
| miss_typo | 3 | 0.963 | 1.021 | 0.94 |
| miss_extra_word | 3 | 1.847 | 2.341 | 0.79 |
| miss_none | 3 | 1.741 | 1.599 | 1.09 |

1-minute load average at the start of each pass: 28.56, 19.23, 43.28.
Peak RSS of the MCP child, MB: tree 125, baseline 126.
