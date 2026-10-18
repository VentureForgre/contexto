## Store size

| N | dialect | pages | MB on disk | page tokens min / median / max | total page tokens | write ms/page |
|---:|---|---:|---:|---|---:|---:|
| 17 | messages | 17 | 0.8 | 20,002 / 23,951 / 27,932 | 407,866 | 1.62 |
| 100 | messages | 100 | 4.3 | 19,530 / 22,975 / 27,950 | 2,304,319 | 1.488 |
| 500 | messages | 500 | 21.1 | 19,326 / 22,814 / 29,684 | 11,495,472 | 1.524 |
| 1000 | messages | 1000 | 42.4 | 19,326 / 22,853 / 29,684 | 23,002,132 | 1.494 |
| 3000 | messages | 3000 | 127.4 | 19,194 / 22,889 / 29,684 | 69,048,523 | 1.478 |
| 6000 | messages | 6000 | 254.7 | 19,194 / 22,882 / 29,684 | 138,094,580 | 1.535 |
| 1000 | responses | 1000 | 40.3 | 150 / 408 / 652 | 409,385 | 1.367 |

## Latency (seconds; median of 5 calls per query, median over queries and 3 passes)

| N | dialect | search rare (stdio) | search common (stdio) | search multi-word (stdio) | recall (stdio) | search rare / common / multi (in-proc) | index_text (in-proc) | MCP startup | MCP peak RSS MB | load1 |
|---:|---|---:|---:|---:|---:|---|---:|---:|---:|---|
| 17 | messages | 0.002 | 0.004 | 0.002 | 0.0005 | 0.002 / 0.004 / 0.002 | 0.035 | 0.053 | 34 | 2.69 |
| 100 | messages | 0.010 | 0.015 | 0.011 | 0.0006 | 0.011 / 0.015 / 0.011 | 0.041 | 0.054 | 40 | 2.63, 2.69 |
| 500 | messages | 0.054 | 0.055 | 0.055 | 0.0005 | 0.054 / 0.056 / 0.055 | 0.077 | 0.055 | 68 | 2.7, 2.8, 2.9 |
| 1000 | messages | 0.108 | 0.105 | 0.110 | 0.0005 | 0.108 / 0.106 / 0.111 | 0.120 | 0.057 | 103 | 3.03, 3.12, 3.27 |
| 3000 | messages | 0.339 | 0.318 | 0.340 | 0.0006 | 0.346 / 0.319 / 0.352 | 0.309 | 0.056 | 245 | 3.03, 3.27, 4.27 |
| 6000 | messages | 0.708 | 0.677 | 0.711 | 0.0006 | 0.722 / 0.673 / 0.745 | 0.626 | 0.073 | 456 | 4.88, 5.63, 5.88 |
| 1000 | responses | 0.116 | 0.119 | 0.119 | 0.0006 | 0.121 / 0.121 / 0.125 | 0.116 | 0.061 | 94 | 9.15, 9.43, 10.17 |

## Search quality: per query (86 probe queries over 30 facts)

| N | dialect | detailed (top 3) | listed (4-53) | absent: buried | absent: query not on page (of which case-insensitive would match / all words present) | value shown in reply | changing: latest page first (of 24) | median matching pages |
|---:|---|---:|---:|---:|---|---:|---:|---:|
| 17 | messages | 61 | 0 | 0 | 25 (6 / 10) | 52 | 19 | 1 |
| 100 | messages | 57 | 4 | 0 | 25 (5 / 9) | 50 | 19 | 1 |
| 500 | messages | 51 | 8 | 1 | 26 (6 / 10) | 45 | 16 | 1 |
| 1000 | messages | 50 | 9 | 2 | 25 (6 / 10) | 44 | 17 | 2 |
| 3000 | messages | 44 | 12 | 4 | 26 (6 / 10) | 40 | 14 | 3 |
| 6000 | messages | 39 | 16 | 6 | 25 (6 / 10) | 36 | 12 | 3 |
| 1000 | responses | 50 | 9 | 2 | 25 (6 / 10) | 41 | 17 | 2 |

## Search quality: per fact, best of its 2-3 queries (30 facts)

| N | dialect | detailed | listed only | absent | static (18) detailed | rename (4) detailed | changing (8) detailed | changing (8) latest first, any query |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 17 | messages | 30 | 0 | 0 | 18 | 4 | 8 | 8 |
| 100 | messages | 29 | 1 | 0 | 17 | 4 | 8 | 8 |
| 500 | messages | 28 | 2 | 0 | 16 | 4 | 8 | 8 |
| 1000 | messages | 28 | 2 | 0 | 16 | 4 | 8 | 8 |
| 3000 | messages | 27 | 2 | 1 | 15 | 4 | 8 | 8 |
| 6000 | messages | 26 | 3 | 1 | 15 | 4 | 7 | 7 |
| 1000 | responses | 28 | 2 | 0 | 16 | 4 | 8 | 8 |
