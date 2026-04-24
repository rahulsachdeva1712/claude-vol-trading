# cache root: C:\Users\Rahul\Documents\claude-vol-trading\data\backtest_cache\rollingoption
# sessions in range: 130 (first=2025-10-24 last=2026-04-23)
# lots/trade: 1
# indices: ['NIFTY', 'BANKNIFTY']

# progress 25/130 sessions (peak so far: Rs 61,157)
# progress 50/130 sessions (peak so far: Rs 61,157)
# progress 75/130 sessions (peak so far: Rs 75,643)
# progress 100/130 sessions (peak so far: Rs 135,200)
# progress 125/130 sessions (peak so far: Rs 135,200)
# progress 130/130 sessions (peak so far: Rs 135,200)

# === Peak concurrent capital (long-option debit) ===

- Sessions with trades: **121**
- Sessions skipped (no legs entered): **9**
- Lots per trade scaled by: **1**

## Distribution

| Stat | Value |
|:--|--:|
| Mean | Rs 42,937 |
| Median (p50) | Rs 37,566 |
| p75 | Rs 56,813 |
| p90 | Rs 83,083 |
| p95 | Rs 100,121 |
| p99 | Rs 107,836 |
| **Max** | **Rs 135,200** |

## Top 15 peak-capital days

| Rank | Date | Peak capital | Peak minute (IST) | Legs open @ peak | Per-leg breakdown |
|--:|:--|--:|:--|--:|:--|
| 1 | 2026-03-09 | Rs 135,200 | 09:54 | 4 | NIFT.CE K=24100 px=484.9; NIFT.PE K=23400 px=450.4; BANK.CE K=56100 px=1333.5; BANK.PE K=54800 px=1146.7 |
| 2 | 2026-03-12 | Rs 108,334 | 10:12 | 4 | NIFT.CE K=23950 px=376.2; NIFT.PE K=23350 px=363.1; BANK.CE K=55600 px=1105.1; BANK.PE K=54400 px=904.2 |
| 3 | 2026-03-13 | Rs 105,844 | 13:13 | 4 | NIFT.CE K=23500 px=368.1; NIFT.PE K=22900 px=355.5; BANK.CE K=54500 px=1020.7; BANK.PE K=53200 px=939.8 |
| 4 | 2026-03-04 | Rs 103,581 | 11:50 | 4 | NIFT.CE K=24700 px=396.0; NIFT.PE K=24050 px=360.0; BANK.CE K=59100 px=1045.9; BANK.PE K=57900 px=768.8 |
| 5 | 2026-03-11 | Rs 103,000 | 14:21 | 4 | NIFT.CE K=24200 px=368.0; NIFT.PE K=23600 px=336.9; BANK.CE K=56500 px=1012.0; BANK.PE K=55300 px=894.0 |
| 6 | 2026-03-10 | Rs 101,728 | 09:59 | 4 | NIFT.CE K=24400 px=363.8; NIFT.PE K=23800 px=347.4; BANK.CE K=57100 px=992.8; BANK.PE K=55900 px=857.5 |
| 7 | 2026-03-06 | Rs 100,121 | 15:07 | 4 | NIFT.CE K=24750 px=367.2; NIFT.PE K=24150 px=328.6; BANK.CE K=58400 px=1015.0; BANK.PE K=57200 px=814.7 |
| 8 | 2026-03-16 | Rs 97,693 | 13:52 | 4 | NIFT.CE K=23400 px=337.1; NIFT.PE K=22800 px=312.3; BANK.CE K=54200 px=979.3; BANK.PE K=53000 px=870.0 |
| 9 | 2026-03-23 | Rs 92,309 | 13:16 | 4 | NIFT.CE K=22800 px=291.4; NIFT.PE K=22200 px=309.9; BANK.CE K=52200 px=909.0; BANK.PE K=51000 px=865.0 |
| 10 | 2026-04-02 | Rs 88,896 | 10:02 | 2 | BANK.CE K=50700 px=1566.0; BANK.PE K=49500 px=1397.2 |
| 11 | 2026-03-05 | Rs 88,392 | 11:56 | 4 | NIFT.CE K=24900 px=326.9; NIFT.PE K=24300 px=314.5; BANK.CE K=59400 px=866.9; BANK.PE K=58100 px=689.9 |
| 12 | 2026-04-01 | Rs 85,304 | 09:40 | 2 | BANK.CE K=52100 px=1510.7; BANK.PE K=50900 px=1332.8 |
| 13 | 2026-03-02 | Rs 83,083 | 14:27 | 4 | NIFT.CE K=25100 px=331.1; NIFT.PE K=24450 px=278.0; BANK.CE K=60300 px=860.0; BANK.PE K=59100 px=589.6 |
| 14 | 2026-04-06 | Rs 82,053 | 09:52 | 2 | BANK.CE K=52100 px=1441.8; BANK.PE K=50800 px=1293.3 |
| 15 | 2026-03-17 | Rs 79,964 | 09:45 | 4 | NIFT.CE K=23750 px=248.0; NIFT.PE K=23150 px=268.5; BANK.CE K=54900 px=788.1; BANK.PE K=53700 px=758.3 |

## Monthly max peak

| Month | Sessions | Max peak capital | Avg peak capital |
|:--|--:|--:|--:|
| 2025-10 | 6 | Rs 56,813 | Rs 29,954 |
| 2025-11 | 19 | Rs 61,157 | Rs 32,292 |
| 2025-12 | 22 | Rs 53,309 | Rs 29,249 |
| 2026-01 | 20 | Rs 75,643 | Rs 34,580 |
| 2026-02 | 20 | Rs 70,574 | Rs 35,425 |
| 2026-03 | 19 | Rs 135,200 | Rs 83,481 |
| 2026-04 | 15 | Rs 88,896 | Rs 51,493 |

## Capital sizing recommendation

- Historical **max** peak (over 121 trading days): Rs 135,200
- 99th percentile: Rs 107,836
- Suggested baseline working capital in Dhan (**max x 1.5 buffer**): **Rs 202,801**
- Aggressive (max x 1.2): Rs 162,241
- Conservative (max x 2.0): Rs 270,401

Notes:
- Numbers are for **1 lot(s) per trade**. Multiply linearly if you run more lots.
- Only long-option debit is counted. No SPAN/ELM margin for shorts (this strategy doesn't sell).
- Capital is the minute-by-minute sum of (entry_price x lot_size) across all simultaneously-open legs in both indices.
- Peak days are rare tail events; the median tells you what you'll actually use most days.
