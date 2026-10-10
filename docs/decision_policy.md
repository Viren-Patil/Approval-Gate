# Decision policy: customer support reply gate

**Team:** Advil · **Persona:** customer support lead at Lumen Cloud (fictional) · **Gated action:** `send_email(to, subject, body)`

Every reply an agent drafts goes through the gate. It is sent only on APPROVE. HOLD_FOR_REVIEW sends it to a manager, and BLOCK means the draft has a wrong fact and must be fixed.

## Signals we use

1. **Our own check against company data.** The wrapper reads `price_sheet.csv` and `policies.json` and checks each sentence of the draft. Every claim it finds is marked verified, contradicted, or unverified, with the file it was checked against.
   - Prices per plan (monthly, annual per month, annual per year, loyalty price)
   - Discount amounts (loyalty 15%, annual billing 20%), which plans they apply to, and stacking
   - Loyalty eligibility from the customer's plan and tenure (`context`)
   - Refund windows (30 days monthly, 14 days annual), first-response times, free trial length, plans and card rules
   - Enterprise has no list price
   - Promises agents can't make: product features, release or delivery dates, policy exceptions, guarantees

   Figures written in words ("nineteen dollars", "within fourteen days") and several facts in one sentence are handled. Nothing matches practice text.
2. **Ezio's per-claim fact-check (`claims[]`).** Covers public-knowledge claims such as laws that our files don't.
3. **Ezio's tone, bias, and agreement scores**, each read separately, since the overall tier can stay GREEN while one of them is very low.
4. **Ezio's overall tier and accuracy status.**

## How a decision is made

The strictest result wins.

| Situation | Decision |
|---|---|
| A claim contradicts our data, or Ezio marks a claim contradicted | BLOCK |
| A promise, exception, or guarantee the agent can't make | HOLD_FOR_REVIEW |
| A dollar amount or % we can't tie to anything in our data (e.g. "$12 setup fee") | HOLD_FOR_REVIEW |
| Tone, bias, or agreement below threshold | HOLD_FOR_REVIEW |
| Ezio tier YELLOW or RED with no specific contradicted claim | HOLD_FOR_REVIEW |
| Everything checks out, or the draft has no checkable claims | APPROVE |

## Thresholds and why

| Setting | Value | Why |
|---|---|---|
| Tone minimum | hold below 60 | Normal friendly support emails score 90+, while several "!!!", all-caps words, or hype words drop tone below 60. That's the over-the-top style rule 4 sends to a manager. |
| Bias minimum | hold below 60 | One loaded word like "clearly" still scores 85. Below 60 the email is pushing one side hard, which a customer-facing reply shouldn't do. |
| Agreement minimum | hold below 60 | A single polite "good question" scores 75 and is fine. Below 60 the email piles on agreement, which is how agents end up confirming a customer's wrong claim. |
| GREEN / YELLOW / RED | APPROVE / HOLD / HOLD | We only BLOCK on a specific contradicted claim. A low overall score alone doesn't prove the email is wrong, and over-blocking costs as much as over-approving. |
| Accuracy `not_scored` | ignore | Most support emails make no public-knowledge claims, and a missing accuracy score is not a reason to block (rule 5). Our own checks cover company facts. |
| Accuracy `unavailable` | HOLD_FOR_REVIEW | With the fact-check down, a wrong public claim (like a law) could go out unseen, so a person reviews first. |
| Unverified figure | HOLD_FOR_REVIEW | A fee or credit we can't find in our data may be made up, so a manager confirms it before the customer sees it. |

## When things fail

- **Ezio down, erroring, or returning a malformed response:** HOLD_FOR_REVIEW. We fail closed and never approve without Ezio's signals.
- **Rate limits (429) and 503s:** the client retries with backoff, up to 3 retries, within a 45-second budget. If it still fails, HOLD_FOR_REVIEW.
- **Slow Ezio:** each call has a timeout. Once it answers, the decision runs as normal.
- **Missing customer context** (no plan or tenure) when a loyalty discount is offered: HOLD_FOR_REVIEW, since eligibility can't be confirmed.

Every decision returns a reason and a list of evidence (each checked claim with its status and source, plus Ezio's scores), and is written to `wrapper/audit_log.jsonl`.

## Results

- **Practice set:** 16/16 exact, 8/8 bad caught, 0/8 good blocked, failure drills 15/15
- **Final set:** 50.0/50, 6/6 bad caught, 0/6 good falsely blocked, 12/12 exact, 6/6 stretch, 6/6 drills
