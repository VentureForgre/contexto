## Store size

| N | dialect | pages | MB on disk | page tokens min / median / max | total page tokens | write ms/page |
|---:|---|---:|---:|---|---:|---:|
| 17 | messages | 17 | 0.8 | 20,002 / 23,951 / 27,932 | 407,866 | 1.452 |
| 1000 | messages | 1000 | 42.4 | 19,326 / 22,853 / 29,684 | 23,002,132 | 1.37 |
| 3000 | messages | 3000 | 127.4 | 19,194 / 22,889 / 29,684 | 69,048,523 | 1.364 |
| 6000 | messages | 6000 | 254.7 | 19,194 / 22,882 / 29,684 | 138,094,580 | 1.359 |
| 1000 | responses | 1000 | 40.3 | 150 / 408 / 652 | 409,385 | 1.21 |

## Latency (seconds; median of 5 calls per query, median over queries and 3 passes)

| N | dialect | search rare (stdio) | search common (stdio) | search multi-word (stdio) | recall (stdio) | search rare / common / multi (in-proc) | index_text (in-proc) | MCP startup | MCP peak RSS MB | load1 |
|---:|---|---:|---:|---:|---:|---|---:|---:|---:|---|
| 17 | messages | 0.001 | 0.006 | 0.002 | 0.0006 | 0.001 / 0.006 / 0.001 | 0.000 | 0.051 | 32 | 2.12 |
| 1000 | messages | 0.014 | 0.024 | 0.037 | 0.0006 | 0.014 / 0.024 / 0.036 | 0.003 | 0.052 | 74 | 2.11 |
| 3000 | messages | 0.110 | 0.108 | 0.176 | 0.0005 | 0.110 / 0.110 / 0.176 | 0.011 | 0.054 | 90 | 2.18 |
| 6000 | messages | 0.240 | 0.222 | 0.372 | 0.0005 | 0.240 / 0.222 / 0.372 | 0.025 | 0.055 | 134 | 2.06 |
| 1000 | responses | 0.015 | 0.027 | 0.037 | 0.0007 | 0.014 / 0.027 / 0.037 | 0.004 | 0.056 | 74 | 1.91 |

## Search quality: per query (86 probe queries over 30 facts)

| N | dialect | detailed (top 3) | listed (4-53) | absent: buried | absent: query not on page (of which case-insensitive would match / all words present) | value shown in reply | changing: latest page first (of 24) | median matching pages |
|---:|---|---:|---:|---:|---|---:|---:|---:|
| 17 | messages | 71 | 0 | 0 | 15 (0 / 0) | 61 | 21 | 1 |
| 1000 | messages | 63 | 7 | 1 | 15 (0 / 0) | 57 | 21 | 3 |
| 3000 | messages | 60 | 6 | 4 | 16 (0 / 0) | 54 | 21 | 6 |
| 6000 | messages | 58 | 8 | 5 | 15 (0 / 0) | 52 | 20 | 12 |
| 1000 | responses | 63 | 7 | 1 | 15 (0 / 0) | 54 | 21 | 3 |

## Search quality: per fact, best of its 2-3 queries (30 facts)

| N | dialect | detailed | listed only | absent | static (18) detailed | rename (4) detailed | changing (8) detailed | changing (8) latest first, any query |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 17 | messages | 30 | 0 | 0 | 18 | 4 | 8 | 8 |
| 1000 | messages | 29 | 1 | 0 | 17 | 4 | 8 | 8 |
| 3000 | messages | 29 | 0 | 1 | 17 | 4 | 8 | 8 |
| 6000 | messages | 29 | 0 | 1 | 17 | 4 | 8 | 8 |
| 1000 | responses | 29 | 1 | 0 | 17 | 4 | 8 | 8 |
