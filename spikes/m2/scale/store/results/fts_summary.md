## Prototype speed (seconds, in-process; median of 5 calls per query, median over the 3 queries)

| N | approach | rare | common | multi-word | result sets equal to today's | one-time cost |
|---:|---|---:|---:|---:|---|---|
| 1000 | current search | 0.1120 | 0.1028 | 0.1094 | n/a |  |
| 1000 | text cache search | 0.0107 | 0.0012 | 0.0078 | yes | load 0.18 s, 33 MB held |
| 1000 | fts5 trigram search | 0.0001 | 0.0061 | 0.0003 | yes | 1.73 ms insert+commit per page, index 106 MB |
| 6000 | current search | 0.7009 | 0.6453 | 0.7242 | n/a |  |
| 6000 | text cache search | 0.0645 | 0.0069 | 0.0480 | yes | load 1.06 s, 196 MB held |
| 6000 | fts5 trigram search | 0.0001 | 0.0390 | 0.0006 | yes | 1.74 ms insert+commit per page, index 632 MB |

## Prototype quality: where the planted (latest) page lands, per probe query

| N | ordering | in top 3 | in top 53 | changing: first value page in the top 3 is the latest (of 24) | facts with a query in the top 3 (of 30) |
|---:|---|---:|---:|---:|---:|
| 1000 | today: exact case-sensitive substring, newest first | 50 | 59 | 17 | 28 |
| 1000 | all words, case-insensitive, newest first | 55 | 68 | n/a | 29 |
| 1000 | all words, case-insensitive, BM25 (no recency) | 63 | 69 | 2 | 30 |
| 6000 | today: exact case-sensitive substring, newest first | 39 | 55 | 12 | 26 |
| 6000 | all words, case-insensitive, newest first | 43 | 63 | n/a | 27 |
| 6000 | all words, case-insensitive, BM25 (no recency) | 57 | 67 | 7 | 29 |
