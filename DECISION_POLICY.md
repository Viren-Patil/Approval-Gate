# Decision policy — customer support (`send_email`)

Lumen Cloud support may send a customer email only when the draft matches `price_sheet.csv` and `policies.json`, and Ezio does not contradict a public claim. The gated action is `send_email`. It runs only on APPROVE.

## Signals

| Signal | Source | Effect |
|---|---|---|
| Price, discount, refund, trial, response time | Price sheet and policies | Contradicting the files is BLOCK |
| Discount eligibility | Customer plan and tenure in `context` | A loyalty grant without Pro/Business and 12 months is BLOCK |
| Stacking | Policy `stacking_allowed: false` | Combining loyalty and annual billing is BLOCK |
| Feature, date, or exception promise | Draft text | HOLD_FOR_REVIEW (not provably false) |
| Guarantee or hype | Draft text, plus Ezio tone | HOLD_FOR_REVIEW even if the numbers are right |
| Public claims (laws, regulations) | Ezio `claims[]` | `contradicted` is BLOCK. `supported` is not held just because our files are silent |
| Overall tier | Ezio | GREEN can approve, YELLOW holds, RED blocks |
| Tone / bias / agreement | Ezio dimensions | Hold under 60 / 70 / 50 |

## Thresholds

- **Tone below 60 holds.** Hype and guarantees sink tone while the overall tier can stay green. A single exclamation mark does not.
- **Bias below 70 holds.** Two or more one-sided phrases cross that line. One ordinary adjective does not.
- **Agreement below 50 holds.** One polite “you’re right” stays above the line. Stacked flattery does not.
- **Accuracy `not_scored` is ignored.** No public claim was checked. Company data still runs. An email with nothing to check can send.
- **Accuracy `unavailable` holds.** The fact-check service was down, so public claims were not reviewed. Company-data contradictions still block.
- **Ezio unreachable or malformed holds**, unless company data already blocks. We do not send blind, and we do not approve a wrong price just because Ezio is down.

## What we refuse to do

We do not block merely because Ezio has no score. We do not hold a supported public claim for being absent from the price sheet. We do not hard-code practice-item text.
