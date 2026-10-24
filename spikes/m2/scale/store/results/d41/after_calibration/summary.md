## Store size

| N | dialect | pages | MB on disk | page tokens min / median / max | total page tokens | write ms/page |
|---:|---|---:|---:|---|---:|---:|
| 17 | messages | 17 | 0.4 | 2,170 / 8,214 / 43,211 | 185,335 | 0.982 |
| 1000 | messages | 1000 | 30.0 | 113 / 5,439 / 567,456 | 15,805,077 | 1.09 |
| 3000 | messages | 3000 | 87.9 | 112 / 5,390 / 568,541 | 46,108,541 | 1.084 |
| 6000 | messages | 6000 | 173.8 | 112 / 5,508 / 571,844 | 91,068,633 | 1.07 |
| 1000 | responses | 1000 | 28.5 | 109 / 284 / 4,853 | 353,145 | 1.023 |

## Latency (seconds; median of 5 calls per query, median over queries and 3 passes)

| N | dialect | search rare (stdio) | search common (stdio) | search multi-word (stdio) | recall (stdio) | search rare / common / multi (in-proc) | index_text (in-proc) | MCP startup | MCP peak RSS MB | load1 |
|---:|---|---:|---:|---:|---:|---|---:|---:|---:|---|
| 17 | messages | 0.001 | 0.004 | 0.001 | 0.0004 | 0.001 / 0.004 / 0.001 | 0.000 | 0.055 | 32 | 3.09 |
| 1000 | messages | 0.011 | 0.075 | 0.023 | 0.0005 | 0.011 / 0.074 / 0.024 | 0.003 | 0.055 | 80 | 3.09 |
| 3000 | messages | 0.035 | 0.125 | 0.071 | 0.0005 | 0.035 / 0.124 / 0.072 | 0.013 | 0.055 | 147 | 3.5 |
| 6000 | messages | 0.161 | 0.273 | 0.233 | 0.0004 | 0.161 / 0.275 / 0.230 | 0.026 | 0.056 | 145 | 3.32 |
| 1000 | responses | 0.012 | 0.087 | 0.023 | 0.0004 | 0.012 / 0.089 / 0.023 | 0.004 | 0.057 | 78 | 3.55 |

## Search quality: per query (86 probe queries over 30 facts)

| N | dialect | detailed (top 3) | listed (4-53) | absent: buried | absent: query not on page (of which case-insensitive would match / all words present) | value shown in reply | changing: latest page first (of 24) | median matching pages |
|---:|---|---:|---:|---:|---|---:|---:|---:|
| 17 | messages | 71 | 0 | 0 | 15 (0 / 0) | 62 | 21 | 1 |
| 1000 | messages | 63 | 7 | 1 | 15 (0 / 0) | 57 | 21 | 3 |
| 3000 | messages | 60 | 6 | 4 | 16 (0 / 0) | 54 | 21 | 6 |
| 6000 | messages | 58 | 8 | 5 | 15 (0 / 0) | 52 | 20 | 10 |
| 1000 | responses | 63 | 7 | 1 | 15 (0 / 0) | 54 | 21 | 3 |

## Search quality: per fact, best of its 2-3 queries (30 facts)

| N | dialect | detailed | listed only | absent | static (18) detailed | rename (4) detailed | changing (8) detailed | changing (8) latest first, any query |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 17 | messages | 30 | 0 | 0 | 18 | 4 | 8 | 8 |
| 1000 | messages | 29 | 1 | 0 | 17 | 4 | 8 | 8 |
| 3000 | messages | 29 | 0 | 1 | 17 | 4 | 8 | 8 |
| 6000 | messages | 29 | 0 | 1 | 17 | 4 | 8 | 8 |
| 1000 | responses | 29 | 1 | 0 | 17 | 4 | 8 | 8 |
