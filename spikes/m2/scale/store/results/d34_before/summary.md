## Store size

| N | dialect | pages | MB on disk | page tokens min / median / max | total page tokens | write ms/page |
|---:|---|---:|---:|---|---:|---:|
| 17 | messages | 17 | 0.8 | 20,002 / 23,951 / 27,932 | 407,866 | 1.575 |
| 1000 | messages | 1000 | 42.4 | 19,326 / 22,853 / 29,684 | 23,002,132 | 1.495 |
| 3000 | messages | 3000 | 127.4 | 19,194 / 22,889 / 29,684 | 69,048,523 | 1.515 |
| 6000 | messages | 6000 | 254.7 | 19,194 / 22,882 / 29,684 | 138,094,580 | 1.511 |
| 1000 | responses | 1000 | 40.3 | 150 / 408 / 652 | 409,385 | 1.32 |

## Latency (seconds; median of 5 calls per query, median over queries and 3 passes)

| N | dialect | search rare (stdio) | search common (stdio) | search multi-word (stdio) | recall (stdio) | search rare / common / multi (in-proc) | index_text (in-proc) | MCP startup | MCP peak RSS MB | load1 |
|---:|---|---:|---:|---:|---:|---|---:|---:|---:|---|
| 17 | messages | 0.002 | 0.004 | 0.002 | 0.0005 | 0.002 / 0.004 / 0.002 | 0.035 | 0.056 | 34 | 3.61 |
| 1000 | messages | 0.107 | 0.105 | 0.109 | 0.0006 | 0.108 / 0.107 / 0.113 | 0.119 | 0.055 | 103 | 3.75, 3.82, 3.87 |
| 3000 | messages | 0.349 | 0.323 | 0.348 | 0.0006 | 0.339 / 0.318 / 0.349 | 0.319 | 0.059 | 245 | 3.72, 3.85, 3.89 |
| 6000 | messages | 0.694 | 0.659 | 0.715 | 0.0006 | 0.710 / 0.643 / 0.711 | 0.590 | 0.063 | 456 | 3.33, 3.55, 3.66 |
| 1000 | responses | 0.119 | 0.119 | 0.123 | 0.0006 | 0.121 / 0.121 / 0.123 | 0.118 | 0.059 | 94 | 3.07, 3.46, 3.5 |

## Search quality: per query (86 probe queries over 30 facts)

| N | dialect | detailed (top 3) | listed (4-53) | absent: buried | absent: query not on page (of which case-insensitive would match / all words present) | value shown in reply | changing: latest page first (of 24) | median matching pages |
|---:|---|---:|---:|---:|---|---:|---:|---:|
| 17 | messages | 61 | 0 | 0 | 25 (6 / 10) | 52 | 19 | 1 |
| 1000 | messages | 50 | 9 | 2 | 25 (6 / 10) | 44 | 17 | 2 |
| 3000 | messages | 44 | 12 | 4 | 26 (6 / 10) | 40 | 14 | 3 |
| 6000 | messages | 39 | 16 | 6 | 25 (6 / 10) | 36 | 12 | 3 |
| 1000 | responses | 50 | 9 | 2 | 25 (6 / 10) | 41 | 17 | 2 |

## Search quality: per fact, best of its 2-3 queries (30 facts)

| N | dialect | detailed | listed only | absent | static (18) detailed | rename (4) detailed | changing (8) detailed | changing (8) latest first, any query |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 17 | messages | 30 | 0 | 0 | 18 | 4 | 8 | 8 |
| 1000 | messages | 28 | 2 | 0 | 16 | 4 | 8 | 8 |
| 3000 | messages | 27 | 2 | 1 | 15 | 4 | 8 | 8 |
| 6000 | messages | 26 | 3 | 1 | 15 | 4 | 7 | 7 |
| 1000 | responses | 28 | 2 | 0 | 16 | 4 | 8 | 8 |
