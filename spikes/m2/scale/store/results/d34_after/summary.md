## Store size

| N | dialect | pages | MB on disk | page tokens min / median / max | total page tokens | write ms/page |
|---:|---|---:|---:|---|---:|---:|
| 17 | messages | 17 | 0.8 | 20,002 / 23,951 / 27,932 | 407,866 | 1.511 |
| 1000 | messages | 1000 | 42.4 | 19,326 / 22,853 / 29,684 | 23,002,132 | 1.411 |
| 3000 | messages | 3000 | 127.4 | 19,194 / 22,889 / 29,684 | 69,048,523 | 1.423 |
| 6000 | messages | 6000 | 254.7 | 19,194 / 22,882 / 29,684 | 138,094,580 | 1.472 |
| 1000 | responses | 1000 | 40.3 | 150 / 408 / 652 | 409,385 | 1.329 |

## Latency (seconds; median of 5 calls per query, median over queries and 3 passes)

| N | dialect | search rare (stdio) | search common (stdio) | search multi-word (stdio) | recall (stdio) | search rare / common / multi (in-proc) | index_text (in-proc) | MCP startup | MCP peak RSS MB | load1 |
|---:|---|---:|---:|---:|---:|---|---:|---:|---:|---|
| 17 | messages | 0.001 | 0.005 | 0.001 | 0.0005 | 0.001 / 0.005 / 0.001 | 0.000 | 0.051 | 35 | 3.23 |
| 1000 | messages | 0.014 | 0.035 | 0.035 | 0.0005 | 0.014 / 0.035 / 0.036 | 0.003 | 0.053 | 79 | 3.19, 3.29 |
| 3000 | messages | 0.043 | 0.079 | 0.107 | 0.0005 | 0.043 / 0.079 / 0.113 | 0.011 | 0.060 | 165 | 3.06, 3.26, 3.28 |
| 6000 | messages | 0.087 | 0.152 | 0.217 | 0.0006 | 0.087 / 0.150 / 0.217 | 0.025 | 0.064 | 293 | 4.31, 4.44, 4.47 |
| 1000 | responses | 0.014 | 0.041 | 0.036 | 0.0006 | 0.015 / 0.040 / 0.038 | 0.003 | 0.060 | 79 | 4.01 |

## Search quality: per query (86 probe queries over 30 facts)

| N | dialect | detailed (top 3) | listed (4-53) | absent: buried | absent: query not on page (of which case-insensitive would match / all words present) | value shown in reply | changing: latest page first (of 24) | median matching pages |
|---:|---|---:|---:|---:|---|---:|---:|---:|
| 17 | messages | 71 | 0 | 0 | 15 (0 / 0) | 61 | 21 | 1 |
| 1000 | messages | 63 | 7 | 1 | 15 (0 / 0) | 57 | 21 | 3 |
| 3000 | messages | 60 | 6 | 4 | 16 (0 / 0) | 54 | 21 | 6 |
| 6000 | messages | 56 | 10 | 5 | 15 (0 / 0) | 50 | 20 | 12 |
| 1000 | responses | 63 | 7 | 1 | 15 (0 / 0) | 54 | 21 | 3 |

## Search quality: per fact, best of its 2-3 queries (30 facts)

| N | dialect | detailed | listed only | absent | static (18) detailed | rename (4) detailed | changing (8) detailed | changing (8) latest first, any query |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 17 | messages | 30 | 0 | 0 | 18 | 4 | 8 | 8 |
| 1000 | messages | 29 | 1 | 0 | 17 | 4 | 8 | 8 |
| 3000 | messages | 29 | 0 | 1 | 17 | 4 | 8 | 8 |
| 6000 | messages | 29 | 0 | 1 | 17 | 4 | 8 | 8 |
| 1000 | responses | 29 | 1 | 0 | 17 | 4 | 8 | 8 |
