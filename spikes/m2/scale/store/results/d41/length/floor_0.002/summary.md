| page sizes | N | page tokens min / median / max | total page tokens |
|---|---:|---|---:|
| spike | 1,000 | 19,326 / 22,857.5 / 29,684 | 23,016,676 |
| spike | 6,000 | 19,194 / 22,868.0 / 29,684 | 138,076,188 |
| calibration | 1,000 | 113 / 5,510.0 / 567,456 | 15,848,272 |
| calibration | 6,000 | 112 / 5,618.5 / 571,844 | 91,476,767 |

| page sizes | N | variant | top 3 (of 86) | latest first (of 24) | stale only (of 24) | facts (of 30) | value shown |
|---|---:|---|---:|---:|---:|---:|---:|
| spike | 1,000 | page b=0.75 | 59 | 20 | 0 | 27 | 53 |
| spike | 1,000 | page b=0.5 | 59 | 20 | 0 | 27 | 53 |
| spike | 1,000 | page b=0.3 | 60 | 21 | 0 | 27 | 54 |
| spike | 1,000 | page b=0 | 60 | 21 | 0 | 27 | 54 |
| spike | 1,000 | d41 | 60 | 21 | 0 | 27 | 54 |
| spike | 1,000 | texts b=0.75 | 53 | 19 | 0 | 27 | 47 |
| spike | 1,000 | fields b=0.75 | 57 | 21 | 0 | 27 | 51 |
| spike | 1,000 | fields b=0.3 | 60 | 21 | 0 | 27 | 54 |
| spike | 1,000 | floored b=0.75 | 60 | 21 | 0 | 27 | 54 |
| spike | 6,000 | page b=0.75 | 41 | 17 | 0 | 24 | 37 |
| spike | 6,000 | page b=0.5 | 45 | 20 | 0 | 24 | 40 |
| spike | 6,000 | page b=0.3 | 44 | 19 | 0 | 24 | 40 |
| spike | 6,000 | page b=0 | 44 | 19 | 0 | 24 | 40 |
| spike | 6,000 | d41 | 44 | 19 | 0 | 24 | 40 |
| spike | 6,000 | texts b=0.75 | 28 | 12 | 1 | 18 | 26 |
| spike | 6,000 | fields b=0.75 | 34 | 19 | 0 | 20 | 30 |
| spike | 6,000 | fields b=0.3 | 40 | 19 | 0 | 22 | 36 |
| spike | 6,000 | floored b=0.75 | 44 | 19 | 0 | 24 | 40 |
| calibration | 1,000 | page b=0.75 | 56 | 20 | 0 | 27 | 50 |
| calibration | 1,000 | page b=0.5 | 57 | 21 | 0 | 27 | 51 |
| calibration | 1,000 | page b=0.3 | 58 | 21 | 0 | 27 | 52 |
| calibration | 1,000 | page b=0 | 60 | 21 | 0 | 27 | 54 |
| calibration | 1,000 | d41 | 60 | 21 | 0 | 27 | 54 |
| calibration | 1,000 | texts b=0.75 | 52 | 19 | 0 | 27 | 46 |
| calibration | 1,000 | fields b=0.75 | 57 | 21 | 0 | 27 | 51 |
| calibration | 1,000 | fields b=0.3 | 60 | 21 | 0 | 27 | 54 |
| calibration | 1,000 | floored b=0.75 | 60 | 21 | 0 | 27 | 54 |
| calibration | 6,000 | page b=0.75 | 29 | 15 | 2 | 17 | 26 |
| calibration | 6,000 | page b=0.5 | 32 | 17 | 0 | 17 | 27 |
| calibration | 6,000 | page b=0.3 | 35 | 19 | 0 | 19 | 30 |
| calibration | 6,000 | page b=0 | 44 | 19 | 0 | 24 | 40 |
| calibration | 6,000 | d41 | 44 | 19 | 0 | 24 | 40 |
| calibration | 6,000 | texts b=0.75 | 28 | 10 | 0 | 16 | 25 |
| calibration | 6,000 | fields b=0.75 | 34 | 19 | 0 | 20 | 30 |
| calibration | 6,000 | fields b=0.3 | 39 | 19 | 0 | 21 | 35 |
| calibration | 6,000 | floored b=0.75 | 44 | 19 | 0 | 24 | 40 |

Reviewer-2's 13-turn cases (detailed turns for `max_retries`; ok: turn 10, the current value, is shown and comes before turn 4):

| variant | mentions 5, 6, 13 | mentions 6, 13 | mentions 6, 8, 12, 13 | mentions 13 | ok (of 4) |
|---|---|---|---|---|---:|
| page b=0.75 | 13, 6, 5 (no) | 13, 6, 4 (no) | 13, 12, 8 (no) | 13, 10, 4 | 1 |
| page b=0.5 | 13, 6, 5 (no) | 13, 6, 4 (no) | 13, 12, 8 (no) | 13, 10, 4 | 1 |
| page b=0.3 | 13, 6, 5 (no) | 13, 6, 4 (no) | 13, 12, 8 (no) | 13, 10, 4 | 1 |
| page b=0 | 13, 10, 6 | 13, 10, 6 | 13, 12, 10 | 13, 10, 4 | 4 |
| d41 | 13, 10, 6 | 13, 10, 6 | 13, 12, 10 | 13, 10, 4 | 4 |
| texts b=0.75 | 13, 10, 6 | 13, 10, 6 | 13, 12, 10 | 13, 10, 4 | 4 |
| fields b=0.75 | 13, 10, 6 | 13, 10, 6 | 13, 12, 10 | 13, 10, 4 | 4 |
| fields b=0.3 | 13, 10, 6 | 13, 10, 6 | 13, 12, 10 | 13, 10, 4 | 4 |
| floored b=0.75 | 13, 10, 6 | 13, 10, 6 | 13, 12, 10 | 13, 10, 4 | 4 |
