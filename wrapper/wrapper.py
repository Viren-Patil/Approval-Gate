"""Your wrapper. Start with check_source_of_truth() and policy.json.

Flow: call Ezio -> read its signals -> check your persona's source of truth -> apply policy
-> decision (+ reason, evidence) -> audit log -> run the mock action only if approved.

Run one input:  python3 wrapper.py customer_support "Your 30% loyalty discount applies to the Pro plan."
Score yourself: python3 evaluate.py customer_support
"""
import csv
import json
import math
import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import audit
import mock_actions
from ezio_client import EzioError, analyze

HERE = Path(__file__).resolve().parent
PERSONAS = HERE.parent / "personas"
POLICY = json.loads((HERE / "policy.json").read_text())

APPROVE, HOLD, BLOCK = "APPROVE", "HOLD_FOR_REVIEW", "BLOCK"
RANK = {APPROVE: 0, HOLD: 1, BLOCK: 2}


@dataclass
class Signals:
    tier: str
    score: float
    summary: str
    accuracy_status: str
    claims_total: int
    claims_verified: int
    claims_contradicted: int
    tone: float
    bias: float
    agreement: float
    claims: list = field(default_factory=list)


@dataclass
class Finding:
    level: str
    message: str


@dataclass
class Decision:
    decision: str
    reason: str
    evidence: list
    ezio: dict


def parse_signals(resp):
    try:
        verdict, dims = resp["verdict"], resp["dimensions"]
        acc = dims["accuracy"]
        if verdict["tier"] not in ("GREEN", "YELLOW", "RED") or acc["status"] not in ("scored", "not_scored", "unavailable"):
            raise ValueError("invalid tier or accuracy status")
        scores = [verdict["score"]] + [dims[d]["score"] for d in ("tone", "bias", "agreement")]
        if acc["status"] == "scored":
            scores.append(acc["score"])
        if any(isinstance(s, bool) or not isinstance(s, (int, float)) or not math.isfinite(s) or not 0 <= s <= 100 for s in scores):
            raise ValueError("scores must be finite numbers between 0 and 100")
        if not isinstance(verdict["summary"], str) or not isinstance(resp.get("claims", []), list):
            raise ValueError("invalid summary or claims")
        return Signals(
            tier=verdict["tier"], score=verdict["score"], summary=verdict["summary"],
            accuracy_status=acc["status"], claims_total=acc.get("claims_total", 0),
            claims_verified=acc.get("claims_verified", 0), claims_contradicted=acc.get("claims_contradicted", 0),
            tone=dims["tone"]["score"], bias=dims["bias"]["score"], agreement=dims["agreement"]["score"],
            claims=resp.get("claims", []),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise EzioError("malformed mock response", resp) from e


@lru_cache(maxsize=None)
def load_source_of_truth(persona):
    """Every .json and .csv file in the persona folder, keyed by file name (for example sot['policies'])."""
    data = {}
    for f in sorted((PERSONAS / persona).iterdir()):
        if f.stem in ("practice_set", "business_rules"):
            continue
        if f.suffix == ".json":
            data[f.stem] = json.loads(f.read_text())
        elif f.suffix == ".csv":
            with f.open() as handle:
                data[f.stem] = list(csv.DictReader(handle))
    return data


_SMALL = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1000000, "billion": 1000000000}
_NUMBER_WORDS = set(_SMALL) | set(_TENS) | set(_SCALES) | {"and"}
_MONEY_RE = re.compile(
    r"\$\s*(\d+(?:\.\d+)?)(?:\s*(billion|million|thousand|k))?"
    r"|(\d+(?:\.\d+)?)\s*(billion|million|thousand)\b"
    r"|(\d+(?:\.\d+)?)\s*(?:dollars|usd)\b",
    re.I,
)
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent\b)", re.I)
_PLAN_RE = re.compile(r"\b(starter|pro|business|enterprise)\b", re.I)
_HOURS_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*-?\s*hours?\b", re.I)
_DAYS_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*-?\s*days?\b", re.I)
_FEATURE_RE = re.compile(
    r"\b(?:ship|launch|release)\w*\b.{0,60}\b(?:feature|export|integration|capability|functionality)\b"
    r"|\b(?:feature|export|integration)\b.{0,50}\b(?:next month|next week|tomorrow|next quarter|q[1-4]|by friday)\b"
    r"|\bengineering\b.{0,80}\b(?:ship|build|deliver|release)\w*\b"
    r"|\b(?:i'll|i will|we'll|we will)\b.{0,70}\b(?:ship|launch|release)\b"
    r"|\b(?:ship|launch|build|deliver|promise|add)\w*.{0,40}\bcustom\b.{0,24}\b(?:feature|export|integration)\b",
    re.I | re.S,
)
_EXCEPTION_RE = re.compile(
    r"\b(?:make an exception|special exception|one-time exception|just this once"
    r"|waive (?:the |our |this )?(?:policy|fee|rule|limit))\b",
    re.I,
)
_GUARANTEE_RE = re.compile(r"\bguarante(?:e|ed|es)\b", re.I)
_DATE_WORD = (
    r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow"
    r"|next week|next month|next quarter|q[1-4]|end of (?:the )?(?:week|month|quarter))"
)
_PROMISE_RE = re.compile(
    # An explicit promise/guarantee/commitment of something to the customer.
    r"\b(?:i|we)\s+(?:promise|guarantee|assure you|commit)\b"
    # A deliverable said to be ready/live/done/shipped by a specific date.
    r"|\b(?:ready|available|live|done|delivered|launched|shipped|finished|complete)\b"
    r"[^.?!]{0,30}\b(?:by|before|on)\s+" + _DATE_WORD + r"\b"
    # "have/get/roll out X by <date>" style delivery commitments.
    r"|\b(?:have|get|roll(?:ing)? out|deliver|ship|launch|release)\b[^.?!]{0,45}\b(?:by|before|on)\s+"
    + _DATE_WORD + r"\b",
    re.I,
)
_HYPE_RE = re.compile(
    r"\b(?:amazing|incredible|unbelievable|life-changing|revolutionary)\b"
    r"|\byou will love\b|\blove it\b",
    re.I,
)


def _hyphen_numbers(text):
    def repl(match):
        left, right = match.group(1).lower(), match.group(2).lower()
        if (left in _TENS and right in _SMALL) or (left in _NUMBER_WORDS and right in _NUMBER_WORDS):
            return f"{match.group(1)} {match.group(2)}"
        return match.group(0)

    return re.sub(r"\b([A-Za-z]+)-([A-Za-z]+)\b", repl, text)


def _consume_number(words, start):
    total = 0
    current = 0
    seen = False
    index = start
    while index < len(words):
        word = words[index].lower()
        if word == "and" and seen:
            index += 1
            continue
        if word in _SMALL:
            current += _SMALL[word]
            seen = True
        elif word in _TENS:
            current += _TENS[word]
            seen = True
        elif word in _SCALES:
            if current == 0 and total == 0:
                break
            current = (current or 1) * _SCALES[word]
            if _SCALES[word] >= 1000:
                total += current
                current = 0
            seen = True
        else:
            break
        index += 1
    if not seen:
        return None
    return total + current, index


def spell_out_numbers(text):
    """Turn 'nineteen' and 'twenty-four' into digits so later checks share one path."""
    pieces = re.findall(r"[A-Za-z]+|[^A-Za-z]+", _hyphen_numbers(text or ""))
    words = [piece for piece in pieces if piece.isalpha()]
    # Map each alpha piece index back by walking pieces.
    rendered = []
    word_i = 0
    values_at = {}
    while word_i < len(words):
        parsed = _consume_number(words, word_i)
        if not parsed or parsed[1] == word_i:
            word_i += 1
            continue
        value, end = parsed
        values_at[word_i] = (value, end)
        word_i = end
    word_i = 0
    skip_until_word = -1
    for piece in pieces:
        if not piece.isalpha():
            if word_i < skip_until_word:
                continue
            rendered.append(piece)
            continue
        if word_i < skip_until_word:
            word_i += 1
            continue
        if word_i in values_at:
            value, end = values_at[word_i]
            rendered.append(str(int(value)) if float(value).is_integer() else str(value))
            skip_until_word = end
            word_i += 1
            continue
        rendered.append(piece)
        word_i += 1
    return "".join(rendered)


def _close(left, right, tol=0.02):
    return abs(float(left) - float(right)) <= tol


def _as_number(value):
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _scale(raw, unit):
    multipliers = {None: 1, "": 1, "k": 1000, "thousand": 1000, "million": 1000000, "billion": 1000000000}
    return float(raw) * multipliers[(unit or "").lower()]


def extract_money(sentence):
    amounts = []
    for match in _MONEY_RE.finditer(sentence):
        if match.group(1):
            amounts.append(_scale(match.group(1), match.group(2)))
        elif match.group(3):
            amounts.append(_scale(match.group(3), match.group(4)))
        else:
            amounts.append(float(match.group(5)))
    return amounts


def _plans_named(sentence):
    seen = []
    for match in _PLAN_RE.finditer(sentence):
        name = match.group(1).lower()
        if name not in seen:
            seen.append(name)
    return seen


def _negated(text, start):
    prefix = text[max(0, start - 32):start]
    return re.search(r"(?:n't|\b(?:no|not|never|cannot|can't|don't|do not|won't|without))\W*$", prefix, re.I) is not None


def _first_unnegated(pattern, text):
    for match in pattern.finditer(text or ""):
        if not _negated(text, match.start()):
            return match
    return None


def _fmt_money(value):
    value = float(value)
    if abs(value) >= 1000:
        return f"${value:,.2f}".replace(".00", "")
    if _close(value, round(value)):
        return f"${value:.0f}"
    return f"${value:.2f}"


def _grants_discount_to_customer(sentence):
    return re.search(
        r"\b(?:you qualify|you are eligible|you're eligible|your\b|applies to you|you get|you receive|you can use)\b",
        sentence,
        re.I,
    ) is not None


def _stacking_violation(text, discounts):
    if not discounts or discounts.get("stacking_allowed", False):
        return False
    loyalty = re.search(r"\bloyalty\b", text, re.I)
    annual = re.search(r"\bannual\b", text, re.I)
    percent_35 = re.search(r"\b35(?:\.0+)?\s*(?:%|percent\b)", text, re.I)
    denied = re.search(
        r"\b(?:cannot|can't|can not|do not|don't|not|never|no)\b.{0,40}\b(?:stack\w*|combin\w*)"
        r"|\b(?:no stacking|not stacked|cannot be combined|can't be combined|not combined)\b",
        text,
        re.I,
    )
    if denied and not percent_35:
        return False
    if loyalty and annual and re.search(
        r"\b(?:stack\w*|combin\w*|together|on top|plus|as well as|both (?:the )?discounts|and the annual|and your annual)\b",
        text,
        re.I,
    ):
        return True
    return bool(percent_35 and (loyalty or annual or re.search(r"\bdiscount", text, re.I)))


def _load_plans(sot):
    plans = {}
    for row in sot.get("price_sheet") or []:
        name = (row.get("plan") or "").strip()
        if not name:
            continue
        plans[name.lower()] = {
            "name": name,
            "monthly": float(row["monthly_price_usd"]),
            "annual_pm": float(row["annual_price_per_month_usd"]),
            "hours": float(row["first_response_hours"]),
            "trial": float(row["free_trial_days"]),
        }
    return plans


def _check_prices(sentence, plans, context):
    findings = []
    amounts = extract_money(sentence)
    if not amounts:
        return findings
    named = [name for name in _plans_named(sentence) if name != "enterprise"]
    if "enterprise" in _plans_named(sentence):
        shown = ", ".join(_fmt_money(amount) for amount in amounts)
        findings.append(Finding(
            BLOCK,
            f"Enterprise pricing is custom through sales, but the email quotes {shown}.",
        ))
    if not named and re.search(r"\b(?:your|the|our) plan\b", sentence, re.I):
        context_plan = str(context.get("plan") or "").lower()
        if context_plan in plans:
            named = [context_plan]
    annualish = re.search(r"\b(?:annual(?:ly)?|yearly|per year)\b", sentence, re.I) is not None
    monthlyish = re.search(r"\b(?:per month|a month|each month|monthly)\b|/mo\b", sentence, re.I) is not None
    for name in named:
        info = plans[name]
        yearly = round(info["annual_pm"] * 12, 2)
        for amount in amounts:
            quoted = _fmt_money(amount)
            if _close(amount, info["monthly"]):
                if annualish and not monthlyish:
                    findings.append(Finding(
                        BLOCK,
                        f"{info['name']} monthly list price is {_fmt_money(info['monthly'])}, but the email states it as the annual price.",
                    ))
                continue
            if _close(amount, info["annual_pm"]):
                if not annualish:
                    findings.append(Finding(
                        BLOCK,
                        f"{quoted} is {info['name']}'s annual per-month rate, not the {_fmt_money(info['monthly'])} monthly price.",
                    ))
                continue
            if _close(amount, yearly):
                if not annualish:
                    findings.append(Finding(
                        BLOCK,
                        f"{quoted} is a yearly {info['name']} total, and the email does not say it is annual billing.",
                    ))
                continue
            findings.append(Finding(
                BLOCK,
                f"{info['name']} is {_fmt_money(info['monthly'])}/month "
                f"({_fmt_money(info['annual_pm'])}/month annual); the email says {quoted}.",
            ))
    return findings


def _check_discounts(sentence, policies, context):
    findings = []
    discounts = (policies or {}).get("discounts") or {}
    loyalty = discounts.get("loyalty") or {}
    annual = discounts.get("annual_billing") or {}
    percents = [float(match.group(1)) for match in _PERCENT_RE.finditer(sentence)]
    mentions_loyalty = re.search(r"\bloyalty\b", sentence, re.I) is not None
    mentions_annual = re.search(r"\b(?:annual(?:ly)?|yearly|per year|a year)\b", sentence, re.I) is not None
    mentions_discount = (
        mentions_loyalty
        or mentions_annual
        or re.search(r"\bdiscount", sentence, re.I) is not None
        or re.search(r"\d+(?:\.\d+)?\s*(?:%|percent)\s*(?:off|discount)\b", sentence, re.I) is not None
        or re.search(r"\btakes?\s+\d+(?:\.\d+)?\s*(?:%|percent)\s*off\b", sentence, re.I) is not None
    )
    if mentions_discount:
        for percent in percents:
            matches_loyalty = mentions_loyalty and _close(percent, loyalty.get("percent", -1))
            matches_annual = mentions_annual and _close(percent, annual.get("percent", -1))
            matches_either = (not mentions_loyalty and not mentions_annual) and (
                _close(percent, loyalty.get("percent", -1)) or _close(percent, annual.get("percent", -1))
            )
            if matches_loyalty or matches_annual or matches_either:
                continue
            findings.append(Finding(
                BLOCK,
                f"Published discounts are loyalty {loyalty.get('percent')}% and annual billing {annual.get('percent')}%; the email says {percent:g}%.",
            ))
    if mentions_loyalty:
        allowed = {item.lower() for item in loyalty.get("applies_to") or []}
        for name in _plans_named(sentence):
            if name == "enterprise" or name not in allowed:
                pretty = name.capitalize() if name != "pro" else "Pro"
                findings.append(Finding(
                    BLOCK,
                    f"The loyalty discount applies only to {', '.join(loyalty.get('applies_to') or [])}, not {pretty}.",
                ))
        if _grants_discount_to_customer(sentence):
            tenure = _as_number(context.get("customer_tenure_months"))
            plan = str(context.get("plan") or "")
            minimum = loyalty.get("min_tenure_months")
            if tenure is None or not plan:
                findings.append(Finding(
                    HOLD,
                    "The email grants a loyalty discount, but plan or tenure is missing from the customer context.",
                ))
            else:
                if plan.lower() not in allowed:
                    findings.append(Finding(
                        BLOCK,
                        f"This customer is on {plan}, and loyalty discount requires {', '.join(loyalty.get('applies_to') or [])}.",
                    ))
                if minimum is not None and tenure < float(minimum):
                    findings.append(Finding(
                        BLOCK,
                        f"Loyalty discount needs {minimum} months; this customer has {tenure:g}.",
                    ))
    if mentions_annual:
        allowed = {item.lower() for item in annual.get("applies_to") or []}
        for name in _plans_named(sentence):
            if name not in allowed:
                findings.append(Finding(
                    BLOCK,
                    f"Annual billing discount applies to {', '.join(annual.get('applies_to') or [])}, not {name}.",
                ))
    return findings


def _check_refunds(sentence, policies):
    findings = []
    if not re.search(r"\brefund", sentence, re.I):
        return findings
    refunds = (policies or {}).get("refunds") or {}
    monthly_days = refunds.get("monthly_plans_days")
    annual_days = refunds.get("annual_plans_days")
    annual = re.search(r"\bannual\b", sentence, re.I) is not None
    anytime = re.search(r"\b(?:any time|anytime|whenever|no time limit)\b", sentence, re.I) is not None
    first_year = re.search(r"\b(?:first year|within a year|12 months|365 days)\b", sentence, re.I) is not None
    days = [float(match.group(1)) for match in _DAYS_RE.finditer(sentence)]
    if anytime or first_year:
        limit = annual_days if annual else monthly_days
        findings.append(Finding(
            BLOCK,
            f"Refunds are limited to {limit} days on the first payment, not an open-ended or year-long window.",
        ))
    limit = annual_days if annual else monthly_days
    if limit is not None:
        for day in days:
            if day > float(limit):
                kind = "annual" if annual else "monthly"
                findings.append(Finding(
                    BLOCK,
                    f"{kind.capitalize()} plans can be refunded within {limit} days of the first payment, not {day:g} days.",
                ))
    if re.search(r"\b(?:every (?:invoice|payment|renewal)|any (?:invoice|payment)|all payments)\b", sentence, re.I):
        findings.append(Finding(BLOCK, "Refunds cover the first payment only, not later invoices."))
    return findings


def _check_trials(sentence, policies, plans):
    findings = []
    if not re.search(r"\btrial\b", sentence, re.I):
        return findings
    trial = (policies or {}).get("free_trial") or {}
    allowed_days = trial.get("days")
    allowed_plans = {item.lower() for item in trial.get("plans") or []}
    named = _plans_named(sentence)
    days = [float(match.group(1)) for match in _DAYS_RE.finditer(sentence)]
    for name in named:
        info = plans.get(name)
        if info and info["trial"] <= 0:
            findings.append(Finding(BLOCK, f"The {info['name']} plan does not include a free trial."))
        elif name not in allowed_plans and name != "enterprise":
            findings.append(Finding(BLOCK, f"Free trials are only for {', '.join(trial.get('plans') or [])}."))
    if "enterprise" in named:
        findings.append(Finding(BLOCK, "Enterprise does not include the self-serve free trial."))
    if allowed_days is not None:
        for day in days:
            if not _close(day, allowed_days):
                findings.append(Finding(
                    BLOCK,
                    f"The free trial is {allowed_days} days, the email says {day:g} days.",
                ))
    card_required = trial.get("card_required")
    if card_required is False and _first_unnegated(
        re.compile(r"\b(?:(?:credit )?card (?:is )?required|requires? (?:a )?credit card)\b", re.I),
        sentence,
    ):
        findings.append(Finding(BLOCK, "The free trial does not require a card."))
    return findings


def _check_response_times(sentence, plans, context):
    findings = []
    if not re.search(r"\b(?:response|respond|reply time|sla)\b", sentence, re.I):
        return findings
    hours = [float(match.group(1)) for match in _HOURS_RE.finditer(sentence)]
    if not hours:
        return findings
    named = [name for name in _plans_named(sentence) if name in plans]
    if re.search(r"\b(?:every|all|any)\b", sentence, re.I) and not named:
        named = list(plans)
    if not named:
        context_plan = str(context.get("plan") or "").lower()
        if context_plan in plans:
            named = [context_plan]
    if not named:
        findings.append(Finding(HOLD, "A response-time promise is not tied to a plan, so a manager should confirm it."))
        return findings
    for name in named:
        expected = plans[name]["hours"]
        for hour in hours:
            if not _close(hour, expected):
                findings.append(Finding(
                    BLOCK,
                    f"{plans[name]['name']} first response is {expected:g} hours, the email says {hour:g} hours.",
                ))
    return findings


def _check_commitments(text):
    findings = []
    if _first_unnegated(_FEATURE_RE, text) or _first_unnegated(_PROMISE_RE, text):
        findings.append(Finding(
            HOLD,
            "The draft promises a product feature or delivery date, which a support agent cannot commit to.",
        ))
    if _first_unnegated(_EXCEPTION_RE, text):
        findings.append(Finding(
            HOLD,
            "The draft offers an exception beyond support-agent authority and needs a manager.",
        ))
    if _first_unnegated(_GUARANTEE_RE, text) or _first_unnegated(_HYPE_RE, text):
        findings.append(Finding(
            HOLD,
            "Over-the-top or guarantee language has to be rewritten even when the numbers are right.",
        ))
    return findings


def check_source_of_truth(text, context, sot):
    """Compare a customer email with the price sheet and company policies.

    Returns Finding(BLOCK or HOLD_FOR_REVIEW) items. An empty list means the
    company data does not object. Ezio's public-knowledge claims are applied
    separately from the signals, because these files do not cover them.
    """
    context = context or {}
    policies = sot.get("policies") or {}
    plans = _load_plans(sot)
    prepared = spell_out_numbers(text or "")
    findings = []
    if _stacking_violation(prepared, policies.get("discounts") or {}):
        findings.append(Finding(
            BLOCK,
            "Loyalty and annual-billing discounts cannot be stacked.",
        ))
    sentences = re.split(r"(?<=[.!?])\s+|\n+", prepared) or [prepared]
    for sentence in sentences:
        findings.extend(_check_prices(sentence, plans, context))
        findings.extend(_check_discounts(sentence, policies, context))
        findings.extend(_check_refunds(sentence, policies))
        findings.extend(_check_trials(sentence, policies, plans))
        findings.extend(_check_response_times(sentence, plans, context))
    findings.extend(_check_commitments(text or ""))
    unique = []
    seen = set()
    for finding in findings:
        if finding.message in seen:
            continue
        seen.add(finding.message)
        unique.append(finding)
    return unique


def apply_policy(signals, findings, policy):
    decision, reasons = APPROVE, []

    def raise_to(level, reason):
        nonlocal decision
        reasons.append(reason)
        if RANK[level] > RANK[decision]:
            decision = level

    for f in findings:
        raise_to(f.level, f.message)
    for claim in signals.claims or []:
        if isinstance(claim, dict) and claim.get("verdict") == "contradicted":
            raise_to(BLOCK, f"Ezio contradicted a public claim: {claim.get('text') or 'claim'}")
    if signals.claims_contradicted and not any(
        isinstance(claim, dict) and claim.get("verdict") == "contradicted" for claim in (signals.claims or [])
    ):
        raise_to(BLOCK, f"Ezio contradicted {signals.claims_contradicted} claim(s)")
    for dim, rule in policy["dimension_minimums"].items():
        score = getattr(signals, dim)
        if score < rule["hold_below"]:
            raise_to(HOLD, f"{dim} score {score} is below {rule['hold_below']}")
    if signals.accuracy_status == "not_scored" and policy["when_accuracy_not_scored"] != "ignore":
        raise_to(policy["when_accuracy_not_scored"], "accuracy was not scored (no claims checked)")
    if signals.accuracy_status == "unavailable":
        raise_to(policy["when_accuracy_unavailable"], "fact-check was unavailable")
    tier_decision = policy["tier_decisions"][signals.tier]
    if tier_decision != APPROVE:
        raise_to(tier_decision, f"Ezio tier is {signals.tier} ({signals.score})")
    if not reasons:
        reasons.append(f"No issues found (Ezio {signals.tier} {signals.score})")
    return reasons, decision


def _worst(findings):
    decision = APPROVE
    for finding in findings:
        if RANK.get(finding.level, 0) > RANK[decision]:
            decision = finding.level
    return decision


def _evidence(findings, reasons, signals=None):
    evidence = [finding.message for finding in findings]
    if signals is not None:
        for claim in signals.claims or []:
            if isinstance(claim, dict) and claim.get("verdict") == "contradicted":
                message = f"Ezio contradicted a public claim: {claim.get('text') or 'claim'}"
                if message not in evidence:
                    evidence.append(message)
    if not evidence:
        evidence = list(reasons) or ["No company-data objection and no contradicted public claim."]
    return evidence


def decide(text, context=None, persona="customer_support", scenario=None, audit_log=True):
    context = context or {}
    try:
        findings = check_source_of_truth(text, context, load_source_of_truth(persona))
    except (KeyError, TypeError, ValueError) as e:
        findings = [Finding(HOLD, f"Source-of-truth check could not finish: {e}")]
    try:
        response = analyze(text, scenario=scenario)
        signals = parse_signals(response)
    except EzioError as e:
        decision = _worst(findings)
        reasons = [f.message for f in findings]
        if decision == APPROVE:
            decision = POLICY["on_ezio_failure"]
            reasons.append(f"Ezio unavailable ({e}); public claims were not checked, so the email is held")
        else:
            reasons.append(f"Ezio unavailable ({e})")
        result = Decision(decision, "; ".join(reasons), _evidence(findings, reasons),
                          {"error": str(e), "response": e.response})
    else:
        reasons, decision = apply_policy(signals, findings, POLICY)
        result = Decision(
            decision, "; ".join(reasons), _evidence(findings, reasons, signals),
            response,
        )
    if audit_log:
        audit.record(persona, text, context, result)
    return result


def gate(text, context=None, persona="customer_support"):
    result = decide(text, context, persona)
    print(f"{result.decision}: {result.reason}")
    if result.decision == APPROVE:
        mock_actions.run_for_persona(persona, text)
    else:
        print("[GATE] action not executed")
    return result


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit('usage: python3 wrapper.py <persona> "<text>" [context-json]')
    gate(sys.argv[2], json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}, sys.argv[1])
