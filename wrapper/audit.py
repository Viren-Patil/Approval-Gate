"""Audit log: one JSON line per decision. Done for you; extend the fields if your policy needs more."""
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

# gate.py judge/try set GATE_LOG_DIR so evaluation runs never pollute your demo audit sample.
LOG = Path(os.getenv("GATE_LOG_DIR") or Path(__file__).parent) / "audit_log.jsonl"
_lock = threading.Lock()


def recent(limit=25):
    """Most recent audit entries, newest last, for a review view in the UI."""
    if not LOG.exists():
        return []
    rows = []
    for line in LOG.read_text().splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def record_review(persona, text, context, note, prior_decision):
    """A human reviewer's note on a held draft. The email is never sent from here."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "persona": persona,
        "input": text,
        "context": context,
        "decision": "REVIEW_NOTE",
        "reason": note,
        "evidence": [{"source": "reviewer", "status": "note",
                      "message": f"Reviewer note on prior decision {prior_decision}. The send_email action was not run."}],
        "ezio": {},
    }
    with _lock, LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def record(persona, text, context, decision):
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "persona": persona,
        "input": text,
        "context": context,
        "decision": decision.decision,
        "reason": decision.reason,
        "evidence": decision.evidence,
        "ezio": decision.ezio,
    }
    with _lock, LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")
