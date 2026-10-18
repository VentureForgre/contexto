## Store size

| N | dialect | pages | MB on disk | page tokens min / median / max | total page tokens | write ms/page |
|---:|---|---:|---:|---|---:|---:|
| 17 | messages | 17 | 0.8 | 20,002 / 23,951 / 27,932 | 407,866 | 1.765 |
| 1000 | messages | 1000 | 42.4 | 19,326 / 22,857 / 29,684 | 23,016,676 | 1.525 |
| 6000 | messages | 6000 | 254.7 | 19,194 / 22,868 / 29,684 | 138,076,188 | 1.566 |

## Latency (seconds; median of 5 calls per query, median over queries and 3 passes)

| N | dialect | search rare (stdio) | search common (stdio) | search multi-word (stdio) | recall (stdio) | search rare / common / multi (in-proc) | index_text (in-proc) | MCP startup | MCP peak RSS MB | load1 |
|---:|---|---:|---:|---:|---:|---|---:|---:|---:|---|

## Search quality: per query (86 probe queries over 30 facts)

| N | dialect | detailed (top 3) | listed (4-53) | absent: buried | absent: query not on page (of which case-insensitive would match / all words present) | value shown in reply | changing: latest page first (of 24) | median matching pages |
|---:|---|---:|---:|---:|---|---:|---:|---:|
| 17 | messages | 61 | 0 | 0 | 25 (6 / 10) | 52 | 19 | 1 |
| 1000 | messages | 44 | 15 | 2 | 25 (6 / 10) | 38 | 16 | 4 |
| 6000 | messages | 15 | 39 | 7 | 25 (6 / 10) | 13 | 9 | 17 |

## Search quality: per fact, best of its 2-3 queries (30 facts)

| N | dialect | detailed | listed only | absent | static (18) detailed | rename (4) detailed | changing (8) detailed | changing (8) latest first, any query |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 17 | messages | 30 | 0 | 0 | 18 | 4 | 8 | 8 |
| 1000 | messages | 26 | 4 | 0 | 15 | 3 | 8 | 8 |
| 6000 | messages | 12 | 17 | 1 | 4 | 1 | 7 | 7 |
