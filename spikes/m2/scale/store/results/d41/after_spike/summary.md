## Store size

| N | dialect | pages | MB on disk | page tokens min / median / max | total page tokens | write ms/page |
|---:|---|---:|---:|---|---:|---:|
| 17 | messages | 17 | 0.8 | 20,002 / 23,951 / 27,932 | 407,866 | 1.511 |
| 1000 | messages | 1000 | 42.4 | 19,326 / 22,853 / 29,684 | 23,002,132 | 1.447 |
| 3000 | messages | 3000 | 127.4 | 19,194 / 22,889 / 29,684 | 69,048,523 | 1.423 |
| 6000 | messages | 6000 | 254.7 | 19,194 / 22,882 / 29,684 | 138,094,580 | 1.451 |
| 1000 | responses | 1000 | 40.3 | 150 / 408 / 652 | 409,385 | 1.242 |

## Latency (seconds; median of 5 calls per query, median over queries and 3 passes)

| N | dialect | search rare (stdio) | search common (stdio) | search multi-word (stdio) | recall (stdio) | search rare / common / multi (in-proc) | index_text (in-proc) | MCP startup | MCP peak RSS MB | load1 |
|---:|---|---:|---:|---:|---:|---|---:|---:|---:|---|
| 17 | messages | 0.001 | 0.006 | 0.001 | 0.0005 | 0.001 / 0.006 / 0.001 | 0.000 | 0.055 | 32 | 5.96 |
| 1000 | messages | 0.015 | 0.030 | 0.037 | 0.0006 | 0.014 / 0.031 / 0.037 | 0.003 | 0.055 | 75 | 5.65 |
| 3000 | messages | 0.111 | 0.124 | 0.178 | 0.0006 | 0.112 / 0.125 / 0.177 | 0.012 | 0.057 | 90 | 4.94 |
| 6000 | messages | 0.244 | 0.255 | 0.377 | 0.0007 | 0.246 / 0.254 / 0.375 | 0.026 | 0.062 | 134 | 4.0 |
| 1000 | responses | 0.016 | 0.034 | 0.037 | 0.0006 | 0.015 / 0.034 / 0.038 | 0.004 | 0.057 | 74 | 3.87 |

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
