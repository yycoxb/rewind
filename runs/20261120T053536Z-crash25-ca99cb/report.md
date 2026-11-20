# Rewind report -- task `crash25`

- **Status:** `RESUMED`
- **Run id:** `20261120T053536Z-crash25-ca99cb`
- **Completed steps:** 50 / 50
- **Journal records:** 203 (`journal.jsonl`)
- **In-doubt intents at report time:** 0
- **Seed tree:** `e6c057fe5e0faf0265bd6163a7fc83876fc687a7`
- **Last checkpoint tree:** `e41dd5c2a3c82470700e8cc5f8a9890ed03c6926`
- **Agent policy:** deterministic 50-step/30-file plan (not an LLM)

## Recovery decision

Final harness decision: **RESUMED**. Last completed checkpoint: step 50.

At least one file-step intent was found without a result after a crash.  It was classified as in-doubt, then resolved by the content-hash matrix as class-A idempotent work (no blind replay).

## Conflict adjudication

No true conflicts in this run.

## Irreversible effects

No external class-C effect was part of this task run.  The outbox/reconciliation path is exercised by `python3 -m rewind demo` and by `tests/test_outbox.py`.

## Artifacts

- `runs/20261120T053536Z-crash25-ca99cb/journal.jsonl`
- `runs/20261120T053536Z-crash25-ca99cb/trace.txt`
- `runs/20261120T053536Z-crash25-ca99cb/report.md`
- `runs/20261120T053536Z-crash25-ca99cb/final.diff`

## Journal tail

```text
{"idem_key": "crash25:step:0049", "seq": 196, "step": 49, "type": "effect"}
{"idem_key": "crash25:step:0049", "seq": 197, "status": "RESUMED", "step": 49, "type": "result"}
{"seq": 198, "status": "RESUMED", "step": 49, "type": "checkpoint"}
{"idem_key": "crash25:step:0050", "seq": 199, "step": 50, "type": "intent"}
{"idem_key": "crash25:step:0050", "seq": 200, "step": 50, "type": "effect"}
{"idem_key": "crash25:step:0050", "seq": 201, "status": "RESUMED", "step": 50, "type": "result"}
{"seq": 202, "status": "RESUMED", "step": 50, "type": "checkpoint"}
{"seq": 203, "status": "RESUMED", "type": "run_end"}
```
