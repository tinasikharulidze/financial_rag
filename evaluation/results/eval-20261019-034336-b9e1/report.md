# Evaluation: consolidation
Run `eval-20261019-034336-b9e1` at 2026-10-19T03:43:36+04:00

## Metrics

| metric | value |
|---|---|
| parse.instructions | 117 |
| parse.rules | 105 |
| parse.llm | 0 |
| parse.unsupported | 12 |
| parse.unresolved | 0 |
| parse.share_rules | 0.897 |
| parse.share_llm | 0.000 |
| parse.share_unresolved | 0.000 |
| assertions_passed | 13 |
| assertions_total | 13 |

## Items (13)

| assertion | passed | detail |
|---|---|---|
| art9_1 exists in 4964423 and starts at the first amendment's effective date | True | ['2022-05-03'] vs 2022-05-03 |
| art4/p3 changes at V1 and again at V2 | True | ['2021-01-01', '2022-05-03', '2023-07-01'] |
| 2060636: number of distinct version dates is between 2 and amendments+1 | True | 14 dates, 13 amendments |
| 2060636: provision versions are contiguous (no gaps or overlaps) | True | gaps=0, overlaps=0 |
| 2060636: every Provision has at least one version | True |  |
| 4964423: number of distinct version dates is between 2 and amendments+1 | True | 3 dates, 2 amendments |
| 4964423: provision versions are contiguous (no gaps or overlaps) | True | gaps=0, overlaps=0 |
| 4964423: every Provision has at least one version | True |  |
| 5679194: number of distinct version dates is between 2 and amendments+1 | True | 2 dates, 1 amendments |
| 5679194: provision versions are contiguous (no gaps or overlaps) | True | gaps=0, overlaps=0 |
| 5679194: every Provision has at least one version | True |  |
| less than 10% of amendment instructions are unresolved | True | 0/117 |
| 4822603: snapshot act has exactly one version date | True |  |

## per_amendment

```json
{
 "2470749": {
  "base": "2060636",
  "number": "81/04",
  "effective_from": "2014-08-25",
  "rules": 4,
  "unsupported": 1
 },
 "2763210": {
  "base": "2060636",
  "number": "27/04",
  "effective_from": "2015-03-10",
  "rules": 6
 },
 "3778783": {
  "base": "2060636",
  "number": "120/04",
  "effective_from": "2017-08-15",
  "rules": 17,
  "unsupported": 1
 },
 "3922106": {
  "base": "2060636",
  "number": "175/04",
  "effective_from": "2017-12-19",
  "rules": 17,
  "unsupported": 9
 },
 "4298586": {
  "base": "2060636",
  "number": "187/04",
  "effective_from": "2018-08-14",
  "rules": 2
 },
 "4738319": {
  "base": "2060636",
  "number": "243/04",
  "effective_from": "2019-12-20",
  "rules": 1
 },
 "4771809": {
  "base": "2060636",
  "number": "7/04",
  "effective_from": "2020-01-14",
  "rules": 4
 },
 "4857069": {
  "base": "2060636",
  "number": "90/04",
  "effective_from": "2020-04-28",
  "rules": 2
 },
 "5451815": {
  "base": "4964423",
  "number": "46/04",
  "effective_from": "2022-05-03",
  "rules": 13
 },
 "5460365": {
  "base": "2060636",
  "number": "57/04",
  "effective_from": "2022-05-11",
  "rules": 3
 },
 "5679250": {
  "base": "2060636",
  "number": "195/04",
  "effective_from": "2023-01-01",
  "rules": 16
 },
 "5840572": {
  "base": "4964423",
  "number": "135/04",
  "effective_from": "2023-07-01",
  "rules": 6
 },
 "5848782": {
  "base": "5679194",
  "number": "166/04",
  "effective_from": "2023-07-04",
  "rules": 1
 },
 "5969808": {
  "base": "2060636",
  "number": "321/04",
  "effective_from": "2023-11-20",
  "rules": 5
 },
 "5983396": {
  "base": "2060636",
  "number": "335/04",
  "effective_from": "2023-12-01",
  "rules": 5
 },
 "6398384": {
  "base": "2060636",
  "number": "12/04",
  "effective_from": "2025-01-31",
  "rules": 3,
  "unsupported": 1
 }
}
```
