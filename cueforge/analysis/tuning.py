"""Learn per-kind confidence thresholds from the programmer's accept/reject history.

History is stored across projects in user settings. A learned threshold is only
ever *offered*; the programmer applies it.
"""
from __future__ import annotations

from ..core.model import Project, SUGGESTION_KINDS

MIN_DECISIONS = 15
MAX_HISTORY = 5000


def record_decisions(history: list[dict], project: Project, before: dict[str, str]) -> list[dict]:
    """Append decisions that changed since `before` (suggestion id -> status)."""
    for s in project.suggestions:
        old = before.get(s.id, "pending")
        if old == "pending" and s.status in ("accepted", "rejected"):
            history.append({"kind": s.kind, "confidence": s.confidence,
                            "accepted": s.status == "accepted", "label": s.label})
    return history[-MAX_HISTORY:]


def learn_thresholds(history: list[dict], beta: float = 2.0) -> dict[str, dict]:
    """Return {kind: {"threshold", "n", "accept_rate"}} for kinds with enough data.

    Uses F-beta with beta > 1 so that recall matters more than precision: hiding a
    good suggestion costs more than showing one you will reject.
    """
    out: dict[str, dict] = {}
    for kind in SUGGESTION_KINDS:
        items = [(h["confidence"], h["accepted"]) for h in history if h["kind"] == kind]
        if len(items) < MIN_DECISIONS:
            continue
        pos = sum(1 for _, a in items if a)
        if pos == 0 or pos == len(items):
            continue
        best_t, best_f = 0.0, -1.0
        for t in sorted({round(c, 2) for c, _ in items}):
            tp = sum(1 for c, a in items if c >= t and a)
            fp = sum(1 for c, a in items if c >= t and not a)
            fn = pos - tp
            if tp == 0:
                continue
            p = tp / (tp + fp)
            r = tp / (tp + fn)
            f = (1 + beta ** 2) * p * r / (beta ** 2 * p + r)
            if f > best_f + 1e-9:
                best_t, best_f = t, f
        out[kind] = {"threshold": best_t, "n": len(items), "accept_rate": pos / len(items),
                     "score": round(best_f, 3)}
    return out
