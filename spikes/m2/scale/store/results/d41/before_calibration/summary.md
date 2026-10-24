## Store size

| N | dialect | pages | MB on disk | page tokens min / median / max | total page tokens | write ms/page |
|---:|---|---:|---:|---|---:|---:|
| 17 | messages | 17 | 0.4 | 2,170 / 8,214 / 43,211 | 185,335 | 0.981 |
| 1000 | messages | 1000 | 30.0 | 113 / 5,439 / 567,456 | 15,805,077 | 1.172 |
| 3000 | messages | 3000 | 87.9 | 112 / 5,390 / 568,541 | 46,108,541 | 1.089 |
| 6000 | messages | 6000 | 173.8 | 112 / 5,508 / 571,844 | 91,068,633 | 1.081 |
| 1000 | responses | 1000 | 28.5 | 109 / 284 / 4,853 | 353,145 | 0.99 |

## Latency (seconds; median of 5 calls per query, median over queries and 3 passes)

| N | dialect | search rare (stdio) | search common (stdio) | search multi-word (stdio) | recall (stdio) | search rare / common / multi (in-proc) | index_text (in-proc) | MCP startup | MCP peak RSS MB | load1 |
|---:|---|---:|---:|---:|---:|---|---:|---:|---:|---|
| 17 | messages | 0.001 | 0.004 | 0.001 | 0.0005 | 0.001 / 0.004 / 0.001 | 0.000 | 0.053 | 32 | 3.88 |
| 1000 | messages | 0.011 | 0.040 | 0.023 | 0.0004 | 0.011 / 0.039 / 0.023 | 0.003 | 0.054 | 64 | 3.81 |
| 3000 | messages | 0.036 | 0.076 | 0.070 | 0.0004 | 0.035 / 0.075 / 0.072 | 0.013 | 0.054 | 112 | 3.71 |
| 6000 | messages | 0.162 | 0.232 | 0.233 | 0.0004 | 0.163 / 0.234 / 0.235 | 0.027 | 0.056 | 112 | 3.34 |
| 1000 | responses | 0.011 | 0.043 | 0.023 | 0.0005 | 0.012 / 0.043 / 0.023 | 0.003 | 0.056 | 65 | 3.02 |

## Search quality: per query (86 probe queries over 30 facts)

| N | dialect | detailed (top 3) | listed (4-53) | absent: buried | absent: query not on page (of which case-insensitive would match / all words present) | value shown in reply | changing: latest page first (of 24) | median matching pages |
|---:|---|---:|---:|---:|---|---:|---:|---:|
| 17 | messages | 71 | 0 | 0 | 15 (0 / 0) | 62 | 21 | 1 |
| 1000 | messages | 61 | 8 | 2 | 15 (0 / 0) | 55 | 20 | 3 |
| 3000 | messages | 57 | 10 | 3 | 16 (0 / 0) | 51 | 20 | 6 |
| 6000 | messages | 53 | 11 | 7 | 15 (0 / 0) | 47 | 17 | 10 |
| 1000 | responses | 61 | 8 | 2 | 15 (0 / 0) | 52 | 20 | 3 |

## Search quality: per fact, best of its 2-3 queries (30 facts)

| N | dialect | detailed | listed only | absent | static (18) detailed | rename (4) detailed | changing (8) detailed | changing (8) latest first, any query |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 17 | messages | 30 | 0 | 0 | 18 | 4 | 8 | 8 |
| 1000 | messages | 29 | 1 | 0 | 17 | 4 | 8 | 8 |
| 3000 | messages | 29 | 0 | 1 | 17 | 4 | 8 | 8 |
| 6000 | messages | 28 | 1 | 1 | 17 | 4 | 7 | 7 |
| 1000 | responses | 29 | 1 | 0 | 17 | 4 | 8 | 8 |
