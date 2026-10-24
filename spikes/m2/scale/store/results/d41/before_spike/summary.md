## Store size

| N | dialect | pages | MB on disk | page tokens min / median / max | total page tokens | write ms/page |
|---:|---|---:|---:|---|---:|---:|
| 17 | messages | 17 | 0.8 | 20,002 / 23,951 / 27,932 | 407,866 | 1.544 |
| 1000 | messages | 1000 | 42.4 | 19,326 / 22,853 / 29,684 | 23,002,132 | 1.394 |
| 3000 | messages | 3000 | 127.4 | 19,194 / 22,889 / 29,684 | 69,048,523 | 1.414 |
| 6000 | messages | 6000 | 254.7 | 19,194 / 22,882 / 29,684 | 138,094,580 | 1.398 |
| 1000 | responses | 1000 | 40.3 | 150 / 408 / 652 | 409,385 | 1.239 |

## Latency (seconds; median of 5 calls per query, median over queries and 3 passes)

| N | dialect | search rare (stdio) | search common (stdio) | search multi-word (stdio) | recall (stdio) | search rare / common / multi (in-proc) | index_text (in-proc) | MCP startup | MCP peak RSS MB | load1 |
|---:|---|---:|---:|---:|---:|---|---:|---:|---:|---|
| 17 | messages | 0.001 | 0.006 | 0.001 | 0.0005 | 0.001 / 0.006 / 0.001 | 0.000 | 0.077 | 39 | 3.91 |
| 1000 | messages | 0.015 | 0.042 | 0.037 | 0.0006 | 0.015 / 0.038 / 0.037 | 0.003 | 0.054 | 75 | 3.76 |
| 3000 | messages | 0.111 | 0.151 | 0.176 | 0.0005 | 0.112 / 0.153 / 0.177 | 0.012 | 0.058 | 90 | 3.78 |
| 6000 | messages | 0.243 | 0.307 | 0.376 | 0.0005 | 0.244 / 0.320 / 0.376 | 0.027 | 0.057 | 134 | 3.76 |
| 1000 | responses | 0.015 | 0.041 | 0.038 | 0.0006 | 0.016 / 0.041 / 0.037 | 0.003 | 0.059 | 74 | 6.22 |

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
