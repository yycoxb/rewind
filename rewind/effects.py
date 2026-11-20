"""Class-B effects: applied work can be compensated on recovery."""

from __future__ import annotations

from typing import Callable

from .journal import INTENT, RESULT, Journal


def execute_compensatable(
    journal: Journal,
    *,
    idem_key: str,
    payload,
    apply: Callable[[object], None],
) -> None:
    journal.append(
        INTENT,
        op="compensatable_effect",
        effect_class="B",
        idem_key=idem_key,
        payload=payload,
        note="result not yet recorded; recovery must compensate before retry",
    )
    apply(payload)
    journal.append(
        RESULT,
        op="compensatable_effect",
        effect_class="B",
        idem_key=idem_key,
        kind="applied",
        status="applied",
    )


def recover_compensatable(
    journal: Journal,
    *,
    idem_key: str,
    compensate: Callable[[object], None],
) -> bool:
    """Compensate one in-doubt class-B effect.  Returns True if work was done."""

    pending = [
        r
        for r in journal.find_in_doubt()
        if r.get("idem_key") == idem_key and r.get("effect_class") == "B"
    ]
    if not pending:
        return False
    for intent in pending:
        compensate(intent.get("payload"))
        journal.append(
            RESULT,
            op="compensatable_effect",
            effect_class="B",
            idem_key=idem_key,
            kind="compensated",
            status="compensated",
            note="local compensation applied during recovery",
        )
    return True
