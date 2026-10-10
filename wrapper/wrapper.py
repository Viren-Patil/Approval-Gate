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
from dataclasses import asdict, dataclass, field
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

VERIFIED, CONTRADICTED, UNVERIFIED = "verified", "contradicted", "unverified"
STATUS_LEVEL = {VERIFIED: APPROVE, CONTRADICTED: BLOCK, UNVERIFIED: HOLD}


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
    """One checked claim. level is the decision it pushes toward (APPROVE means it checked out)."""
    level: str
    message: str
    claim: str = ""
    status: str = ""
    source: str = ""


def finding(status, claim, message, source="", level=None):
    return Finding(level or STATUS_LEVEL[status], message, claim.strip(), status, source)


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


# ---- Text parsing helpers -------------------------------------------------

SMALL_NUMBERS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_NUMBER_WORD = "|".join(sorted(list(SMALL_NUMBERS) + ["hundred", "thousand"], key=len, reverse=True))
NUMBER_RUN = re.compile(rf"\b(?:{_NUMBER_WORD})(?:[\s-]+(?:and[\s-]+)?(?:{_NUMBER_WORD}))*\b", re.I)

NUM = r"(\d[\d,]*(?:\.\d+)?)"
MONEY = re.compile(rf"\$\s?{NUM}|{NUM}\s*(?:dollars?|usd|bucks)\b", re.I)
PERCENT = re.compile(rf"{NUM}\s*(?:%|percent\b)", re.I)
DURATION = re.compile(rf"{NUM}\s*-?\s*(hours?|hrs?|days?|weeks?|months?|years?)\b", re.I)
ONE_UNIT = re.compile(r"\b(?:(?:the\s+)?first|(?:within|in|for|up to|after)\s+(?:a|an|one))\s+(hour|day|week|month|year)\b", re.I)
UNIT_DAYS = {"h": 1 / 24, "d": 1, "w": 7, "m": 30, "y": 365}
PRICE_PERIOD = re.compile(r"^\s*(?:/\s*(?:mo|month|yr|year)\b|(?:per|a|an|each|every)\s+(?:month|year|annum)\b|monthly\b|annually\b|yearly\b)", re.I)

NEGATION = re.compile(r"\b(?:not|no|never|cannot|can not|without|unable)\b|n't\b", re.I)
COMBINE = re.compile(r"\b(?:combin\w*|stack\w*|together|on top of|plus|both)\b", re.I)
DISCOUNT_WORDS = re.compile(r"\b(?:off|discount\w*|save|savings|reduc\w*|cheaper)\b", re.I)
ANNUAL_WORDS = re.compile(r"\b(?:annual\w*|yearly|per year|a year)\b", re.I)
MONTHLY_WORDS = re.compile(r"\b(?:monthly|month-to-month)\b", re.I)
ADDRESSED = re.compile(r"\byou(?:r|'re|'ve|'ll)?\b", re.I)
ALL_PLANS = re.compile(r"\b(?:every|all|each|any)\s+(?:(?:self-serve|paid|of our)\s+)?plans?\b", re.I)

COMMITMENT = re.compile(r"(?:\b(?:will|going to|promis\w*|make sure|ensure|commit(?:ted)?)\b|'ll\b)", re.I)
PRODUCT_CHANGE = re.compile(r"\b(?:features?|roadmap|ship\w*|releas\w*|launch\w*|build\w*|add (?:it|this|that|support)|integration)\b", re.I)
DELIVERY = re.compile(r"\b(?:deliver\w*|arriv\w*)\b", re.I)
EXCEPTION = re.compile(r"\b(?:exception|waive\w*|bend the rules|just this once|special case|override)\b", re.I)
GUARANTEE = re.compile(r"\bguarantee\w*\b", re.I)
ANY_TIME = re.compile(r"\b(?:any ?time|whenever|no matter when|forever|lifetime|always)\b", re.I)
RESPONSE_WORDS = re.compile(r"\b(?:respon(?:se|d)\w*|repl(?:y|ies))\b", re.I)
# A deliverable said to be ready/live/done by a specific date is a delivery-date promise (rule 3),
# e.g. "have the new dashboard ready by Friday". Communication promises ("send an update by end of
# day") are not caught, because they lack a deliverable-state word.
_DATE_WORD = (r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|next week"
              r"|next month|next quarter|q[1-4]|end of (?:the )?(?:week|month|quarter))")
DELIVERY_DATE = re.compile(
    r"\b(?:ready|available|live|done|finished|complete|completed|delivered|launched|shipped|rolled out|up and running)\b"
    r"[^.?!]{0,30}\b(?:by|before|on)\s+" + _DATE_WORD + r"\b",
    re.I,
)


def words_to_digits(text):
    """'nineteen dollars' -> '19 dollars', so every figure is parsed the same way."""
    def convert(match):
        total, current = 0, 0
        for word in re.split(r"[\s-]+", match.group(0).lower()):
            if word in SMALL_NUMBERS:
                current += SMALL_NUMBERS[word]
            elif word == "hundred":
                current = max(current, 1) * 100
            elif word == "thousand":
                total, current = total + max(current, 1) * 1000, 0
        return str(total + current)
    return NUMBER_RUN.sub(convert, text)


def sentences(text):
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def to_number(raw):
    return float(raw.replace(",", ""))


def money_amounts(sentence):
    return [(to_number(m.group(1) or m.group(2)), m.start(), m.end()) for m in MONEY.finditer(sentence)]


def percents(sentence):
    return [(to_number(m.group(1)), m.start()) for m in PERCENT.finditer(sentence)]


def durations_in_days(sentence):
    found = [(to_number(m.group(1)) * UNIT_DAYS[m.group(2)[0].lower()], m.start()) for m in DURATION.finditer(sentence)]
    found += [(UNIT_DAYS[m.group(1)[0].lower()], m.start()) for m in ONE_UNIT.finditer(sentence)]
    return found


def mentions(pattern, sentence):
    return [m.start() for m in re.finditer(pattern, sentence)]


def nearest(position, named):
    """named: list of (position, name). Return the name mentioned closest to position."""
    return min(named, key=lambda pair: abs(pair[0] - position))[1] if named else None


def negated(sentence):
    return bool(NEGATION.search(sentence))


def same_amount(a, b):
    return abs(a - b) < 0.005


def fmt(value):
    return f"{value:g}"


# ---- Persona data access ---------------------------------------------------

class Facts:
    """The customer_support source of truth plus the customer's context, in one place."""

    def __init__(self, sot, context):
        self.policies = sot["policies"]
        self.plans = {row["plan"]: row for row in sot["price_sheet"]}
        self.plan_names = list(self.plans) + (["Enterprise"] if "enterprise" in self.policies else [])
        self.discounts = self.policies["discounts"]
        plan = context.get("plan")
        self.customer_plan = plan if plan in self.plan_names else None
        self.tenure = self._months(context.get("customer_tenure_months"))

    @staticmethod
    def _months(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def plans_named(self, sentence):
        """Plan names as (position, name). Capitalized, or followed by 'plan', so 'your business' is not a plan."""
        found = []
        for name in self.plan_names:
            exact = mentions(rf"\b{name}\b", sentence)
            with_plan = [m.start() for m in re.finditer(rf"\b{name}\s+plans?\b", sentence, re.I)]
            found += [(pos, name) for pos in sorted(set(exact + with_plan))]
        return found

    def plan_for(self, position, named):
        return nearest(position, named) or self.customer_plan

    def known_prices(self, plan):
        row = self.plans[plan]
        monthly, annual = float(row["monthly_price_usd"]), float(row["annual_price_per_month_usd"])
        prices = {monthly: "monthly price", annual: "price per month on annual billing",
                  round(annual * 12, 2): "yearly price on annual billing"}
        loyalty = self.discounts["loyalty"]
        if plan in loyalty["applies_to"]:
            prices[round(monthly * (1 - loyalty["percent"] / 100), 2)] = "monthly price after the loyalty discount"
        return prices

    def discount_kinds(self):
        return [k for k, v in self.discounts.items() if isinstance(v, dict) and "percent" in v]

    def stacked_percents(self):
        """Percentages a stacked discount would produce (simple sum and compounded)."""
        values = [self.discounts[k]["percent"] for k in self.discount_kinds()]
        if len(values) < 2:
            return set()
        remaining = math.prod(1 - v / 100 for v in values)
        return {sum(values), round((1 - remaining) * 100, 2)}


# ---- Individual checks (each returns a list of Finding) -------------------

def check_prices(sentence, facts):
    findings, named = [], facts.plans_named(sentence)
    for amount, start, end in money_amounts(sentence):
        is_price = bool(named) or bool(PRICE_PERIOD.match(sentence[end:]))
        plan = facts.plan_for(start, named) if is_price else None
        if plan is None:
            findings.append(finding(UNVERIFIED, sentence, f"${fmt(amount)} is not a price we can match to a plan in the price sheet."))
        elif plan not in facts.plans:
            findings.append(finding(CONTRADICTED, sentence, f"{plan} pricing is {facts.policies['enterprise']['pricing']} (contact {facts.policies['enterprise']['contact']}); the email quotes ${fmt(amount)}.", "policies.json"))
        else:
            findings.append(price_finding(sentence, plan, amount, facts.known_prices(plan)))
    return findings


def price_finding(sentence, plan, amount, prices):
    for price, label in prices.items():
        if same_amount(price, amount):
            return finding(VERIFIED, sentence, f"{plan} {label} is ${fmt(price)}.", "price_sheet.csv")
    listed = ", ".join(f"{label} ${fmt(p)}" for p, label in prices.items())
    return finding(CONTRADICTED, sentence, f"The email says {plan} is ${fmt(amount)}; the price sheet has {listed}.", "price_sheet.csv")


def discount_mentions(sentence, facts):
    found = [(pos, "loyalty") for pos in mentions(r"(?i)\bloyal\w*", sentence)]
    found += [(m.start(), "annual_billing") for m in ANNUAL_WORDS.finditer(sentence)]
    return [(pos, kind) for pos, kind in found if kind in facts.discounts]


def check_stacking(sentence, facts):
    kinds = {kind for _, kind in discount_mentions(sentence, facts)}
    if len(kinds) < 2 or not COMBINE.search(sentence) or facts.discounts.get("stacking_allowed", True):
        return []
    if negated(sentence):
        return [finding(VERIFIED, sentence, "Discounts cannot be stacked, and the email says so.", "policies.json")]
    return [finding(CONTRADICTED, sentence, "The email combines the loyalty and annual billing discounts; policy says discounts cannot be stacked.", "policies.json")]


def check_percents(sentence, facts):
    findings, kinds = [], discount_mentions(sentence, facts)
    for value, position in percents(sentence):
        kind = nearest(position, kinds)
        findings.append(percent_finding(sentence, value, kind, facts))
    return findings


def percent_finding(sentence, value, kind, facts):
    policy_values = {facts.discounts[k]["percent"]: k for k in facts.discount_kinds()}
    if kind and same_amount(value, facts.discounts[kind]["percent"]):
        return finding(VERIFIED, sentence, f"The {kind.replace('_', ' ')} discount is {fmt(value)}%.", "policies.json")
    if any(same_amount(value, s) for s in facts.stacked_percents()) and not facts.discounts.get("stacking_allowed", True):
        return finding(CONTRADICTED, sentence, f"{fmt(value)}% only comes from stacking discounts; policy says discounts cannot be stacked.", "policies.json")
    if kind:
        actual = facts.discounts[kind]["percent"]
        return finding(CONTRADICTED, sentence, f"The {kind.replace('_', ' ')} discount is {fmt(actual)}%; the email says {fmt(value)}%.", "policies.json")
    if value in policy_values:
        return finding(VERIFIED, sentence, f"{fmt(value)}% matches the {policy_values[value].replace('_', ' ')} discount.", "policies.json")
    if DISCOUNT_WORDS.search(sentence):
        return finding(CONTRADICTED, sentence, f"{fmt(value)}% off is not a discount in the policy, and agents cannot offer discounts beyond policy.", "policies.json")
    return finding(UNVERIFIED, sentence, f"{fmt(value)}% does not match anything in the price sheet or policies.")


def check_discount_plans(sentence, facts):
    """A discount named next to a plan it does not apply to."""
    findings, named = [], facts.plans_named(sentence)
    if ALL_PLANS.search(sentence) or negated(sentence):
        return findings
    for position, kind in discount_mentions(sentence, facts):
        if not (DISCOUNT_WORDS.search(sentence) or PERCENT.search(sentence)):
            continue
        plan = nearest(position, named)
        allowed = facts.discounts[kind]["applies_to"]
        if plan and plan not in allowed:
            findings.append(finding(CONTRADICTED, sentence, f"The {kind.replace('_', ' ')} discount applies only to {', '.join(allowed)}, not {plan}.", "policies.json"))
    return findings


def check_loyalty_eligibility(sentence, facts):
    """Rule 2: a loyalty discount offered to this customer needs the right plan and tenure."""
    loyalty = facts.discounts.get("loyalty")
    if not loyalty or not re.search(r"(?i)\bloyal\w*", sentence) or not ADDRESSED.search(sentence) or COMBINE.search(sentence):
        return []
    findings = check_tenure_threshold(sentence, loyalty)
    eligible, why = loyalty_eligibility(facts, loyalty)
    if eligible is None:
        findings.append(finding(UNVERIFIED, sentence, f"Cannot confirm loyalty eligibility: {why}."))
    elif negated(sentence):
        status = CONTRADICTED if eligible else VERIFIED
        findings.append(finding(status, sentence, f"The email says the customer does not qualify for the loyalty discount; {why}.", "policies.json + context"))
    else:
        status = VERIFIED if eligible else CONTRADICTED
        findings.append(finding(status, sentence, f"Loyalty discount offered; {why}.", "policies.json + context"))
    return findings


def loyalty_eligibility(facts, loyalty):
    if facts.customer_plan is None or facts.tenure is None:
        return None, "the customer's plan or tenure is missing"
    plan_ok = facts.customer_plan in loyalty["applies_to"]
    tenure_ok = facts.tenure >= loyalty["min_tenure_months"]
    why = (f"customer is on {facts.customer_plan} (eligible plans: {', '.join(loyalty['applies_to'])}) "
           f"with {fmt(facts.tenure)} months tenure (minimum {loyalty['min_tenure_months']})")
    return plan_ok and tenure_ok, why


def check_tenure_threshold(sentence, loyalty):
    """'after 6 months you get the loyalty discount' states a lower bar than policy."""
    minimum = loyalty["min_tenure_months"]
    findings = []
    for days, _ in durations_in_days(sentence):
        months = days / 30
        if months < minimum and not re.search(r"(?i)\b(?:more than|over|at least)\b", sentence):
            findings.append(finding(CONTRADICTED, sentence, f"Loyalty discount needs {minimum} months of tenure; the email says {fmt(round(months, 1))}.", "policies.json"))
    return findings


def check_refunds(sentence, facts):
    if not re.search(r"(?i)\brefund\w*", sentence):
        return []
    refunds = facts.policies["refunds"]
    allowed = refund_windows(sentence, refunds)
    windows = ", ".join(f"{label} {days} days" for label, days in allowed.items())
    found = durations_in_days(sentence)
    if not found:
        if ANY_TIME.search(sentence) and not negated(sentence):
            return [finding(CONTRADICTED, sentence, f"Refunds have a fixed window ({windows}); the email promises a refund at any time.", "policies.json")]
        return []
    findings = []
    for days, _ in found:
        status = VERIFIED if any(same_amount(days, d) for d in allowed.values()) else CONTRADICTED
        findings.append(finding(status, sentence, f"Email states a {fmt(round(days))}-day refund window; policy: {windows}, on the {refunds['scope']}.", "policies.json"))
    return findings


def refund_windows(sentence, refunds):
    if ANNUAL_WORDS.search(sentence):
        return {"annual plans": refunds["annual_plans_days"]}
    if MONTHLY_WORDS.search(sentence):
        return {"monthly plans": refunds["monthly_plans_days"]}
    return {"monthly plans": refunds["monthly_plans_days"], "annual plans": refunds["annual_plans_days"]}


def check_response_times(sentence, facts):
    if not RESPONSE_WORDS.search(sentence):
        return []
    findings, named = [], facts.plans_named(sentence)
    for days, position in durations_in_days(sentence):
        plan = facts.plan_for(position, named)
        hours = days * 24
        if plan is None or plan not in facts.plans:
            findings.append(finding(UNVERIFIED, sentence, f"A {fmt(round(hours, 2))}-hour response time is stated, but no plan with a published response time is known."))
            continue
        promised = float(facts.plans[plan]["first_response_hours"])
        status = VERIFIED if same_amount(hours, promised) else CONTRADICTED
        findings.append(finding(status, sentence, f"{plan} first response is {fmt(promised)} hours; the email says {fmt(round(hours, 2))}.", "price_sheet.csv"))
    return findings


def check_trials(sentence, facts):
    if not re.search(r"(?i)\btrial\b", sentence):
        return []
    trial, findings = facts.policies["free_trial"], []
    for days, _ in durations_in_days(sentence):
        status = VERIFIED if same_amount(days, trial["days"]) else CONTRADICTED
        findings.append(finding(status, sentence, f"Free trial is {trial['days']} days; the email says {fmt(round(days))}.", "policies.json"))
    if not negated(sentence):
        for _, plan in facts.plans_named(sentence):
            if plan not in trial["plans"]:
                findings.append(finding(CONTRADICTED, sentence, f"Free trials are only on {', '.join(trial['plans'])}, not {plan}.", "policies.json"))
    if re.search(r"(?i)\b(?:credit )?card\b", sentence) and not trial["card_required"] and not negated(sentence):
        findings.append(finding(CONTRADICTED, sentence, "The free trial does not require a card.", "policies.json"))
    return findings


def check_commitments(sentence, facts):
    """Rules 3 and 4: promises and guarantees an agent cannot make go to a manager."""
    authority = facts.policies.get("agent_authority", {})
    findings = []
    promise = COMMITMENT.search(sentence)
    if promise and PRODUCT_CHANGE.search(sentence) and not authority.get("can_promise_product_features", False):
        findings.append(finding(UNVERIFIED, sentence, "Promises a product feature or release; agents cannot promise features.", "policies.json", HOLD))
    if promise and DELIVERY.search(sentence):
        findings.append(finding(UNVERIFIED, sentence, "Promises a delivery date; a manager must approve it.", "business_rules.md", HOLD))
    if DELIVERY_DATE.search(sentence) and not negated(sentence):
        findings.append(finding(UNVERIFIED, sentence, "Promises a deliverable will be ready by a specific date; a manager must approve a delivery commitment.", "business_rules.md", HOLD))
    if EXCEPTION.search(sentence) and not negated(sentence):
        findings.append(finding(UNVERIFIED, sentence, "Offers a policy exception; a manager must approve it.", "business_rules.md", HOLD))
    if GUARANTEE.search(sentence) and not negated(sentence):
        findings.append(finding(UNVERIFIED, sentence, "Makes a guarantee; guarantees need a manager's review.", "business_rules.md", HOLD))
    return findings


SENTENCE_CHECKS = (check_prices, check_stacking, check_percents, check_discount_plans,
                   check_loyalty_eligibility, check_refunds, check_response_times, check_trials,
                   check_commitments)


def check_source_of_truth(text, context, sot):
    """Check every sentence of the email against price_sheet.csv and policies.json.

    Returns a list of Finding: verified claims (level APPROVE, kept as evidence),
    contradicted claims (BLOCK) and claims that need a person (HOLD_FOR_REVIEW).
    """
    if "price_sheet" not in sot or "policies" not in sot:
        return []  # these checks are for the customer_support persona's data only
    facts = Facts(sot, context or {})
    findings = []
    for original in sentences(text):
        sentence = words_to_digits(original)
        for check in SENTENCE_CHECKS:
            findings.extend(check(sentence, facts))
    return unique(findings)


def check_ezio_claims(signals):
    """Rule 6: Ezio sees public-knowledge claims our files don't cover."""
    findings = []
    for claim in signals.claims:
        if not isinstance(claim, dict):
            continue
        text, verdict, source = str(claim.get("text", "")), claim.get("verdict"), str(claim.get("source") or "Ezio")
        if verdict == "contradicted":
            findings.append(finding(CONTRADICTED, text, f"Ezio found this claim contradicted by {source}.", "Ezio"))
        elif verdict == "supported":
            findings.append(finding(VERIFIED, text, f"Ezio found this claim supported by {source}.", "Ezio"))
        else:
            findings.append(finding(UNVERIFIED, text, f"Ezio could not verify this claim ({verdict}).", "Ezio"))
    return findings


def unique(findings):
    seen, result = set(), []
    for f in findings:
        key = (f.level, f.message, f.claim)
        if key not in seen:
            seen.add(key)
            result.append(f)
    return result


def apply_policy(signals, findings, policy):
    decision, reasons = APPROVE, []

    def raise_to(level, reason):
        nonlocal decision
        reasons.append(reason)
        if RANK[level] > RANK[decision]:
            decision = level

    for f in findings:
        if f.level != APPROVE:
            raise_to(f.level, f.message)
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
        verified = sum(f.status == VERIFIED for f in findings)
        checked = f"{verified} claim{'s' if verified != 1 else ''} verified" if verified else "no checkable claims"
        reasons.append(f"No issues found: {checked} (Ezio {signals.tier} {signals.score})")
    return reasons, decision


def evidence_for(findings, signals):
    """Every checked claim, plus the Ezio signals used, so even an APPROVE shows its basis."""
    evidence = [asdict(f) for f in findings]
    evidence.append({
        "source": "Ezio", "status": signals.accuracy_status,
        "message": (f"Ezio tier {signals.tier} ({signals.score}); tone {signals.tone}, bias {signals.bias}, "
                    f"agreement {signals.agreement}; accuracy {signals.accuracy_status}"),
    })
    return evidence


def decide(text, context=None, persona="customer_support", scenario=None, audit_log=True):
    context = context or {}
    try:
        response = analyze(text, scenario=scenario)
        signals = parse_signals(response)
    except EzioError as e:
        # Ezio is down, but our own company-data checks still run. A draft that already
        # contradicts the price sheet or policy is blocked; otherwise we fail closed to a hold.
        findings = check_source_of_truth(text, context, load_source_of_truth(persona))
        decision = POLICY["on_ezio_failure"]
        for f in findings:
            if f.level != APPROVE and RANK[f.level] > RANK[decision]:
                decision = f.level
        actionable = [f.message for f in findings if f.level != APPROVE]
        reason = "; ".join(actionable) if decision != POLICY["on_ezio_failure"] else f"Ezio unavailable: {e}"
        evidence = [asdict(f) for f in findings]
        evidence.append({"source": "Ezio", "status": "error",
                         "message": f"Ezio call failed ({e}); company-data checks still ran, failing closed."})
        result = Decision(decision, reason, evidence, {"error": str(e), "response": e.response})
    else:
        findings = check_source_of_truth(text, context, load_source_of_truth(persona)) + check_ezio_claims(signals)
        reasons, decision = apply_policy(signals, findings, POLICY)
        result = Decision(decision, "; ".join(reasons), evidence_for(findings, signals), response)
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
