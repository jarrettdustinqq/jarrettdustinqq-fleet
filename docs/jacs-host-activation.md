# JACS host-activation package

PR #13 is a validation-only candidate. This package does not merge, deploy, contact
external parties, mutate scheduler tasks, move money, or write canonical JACS.

## Trust boundary

The host runtime cannot call ChatGPT connectors. A trusted connector orchestrator
must therefore perform fresh authoritative reads immediately before each
consequential Control Hub scan and hand those read results to
`ops/jacs_snapshot_exporter.py`.

The exporter is intentionally **not** a connector client. It accepts a pre-collected
export specification containing:

- a fresh canonical JACS/Google-Sheets registry read receipt;
- one aggregated fresh read receipt per `source_kind`;
- facts bound to the `source_kind` of the read that produced them;
- a fresh live scheduler snapshot when automation sync is required;
- the complete required State/fact dependency declaration for the workflow.

Every read receipt has both `observed_at` and `fetched_at`. `observed_at` describes
when the underlying source observation occurred; `fetched_at` proves that the
connector was actually reread within the snapshot's `max_age_seconds`. This permits
an older provider statement to remain authoritative while still requiring a fresh
retrieval of that statement.

`source_ref` is opaque to Fleet. The trusted connector orchestrator must populate it
from the actual connector/tool result identity, never from model-authored prose.

The exporter:

1. rejects stale/future connector fetch receipts;
2. rejects unknown source kinds and duplicate source-kind receipts;
3. binds every emitted fact to its enclosing read source, preventing per-fact
   `source_kind` spoofing;
4. requires every consequential `required_fact_key` to have at least one fresh
   non-predictive source read;
5. requires a fresh scheduler-runtime receipt when live automations are present or
   automation sync is required;
6. constructs the schema-v2 envelope and deterministic SHA-256 digest;
7. validates the constructed bundle with the same boundary validator used by the
   host runtime;
8. atomically writes `jacs_preflight.json` mode `0600`.

The exporter performs **no canonical Google-Sheets/JACS writeback**. Local stale
journals and replay journals remain local provenance only.

## Files

- `ops/jacs_snapshot_exporter.py` — connector-side constructor contract.
- `schemas/jacs-preflight-snapshot-v2.schema.json` — machine-readable snapshot schema.
- `examples/jacs-preflight/` — valid/refused reference-clock examples.
- `config/jacs-host-activation-policy.json` — machine-readable supported-entry policy.
- `tests/jacs_host_activation_harness.py` — intended-host fail-closed proof harness.

The static examples use the reference clock `2026-08-24T08:00:00Z`; they are examples,
not reusable host authorization artifacts.

## Threat model

| Threat | Mitigation | Residual risk |
| --- | --- | --- |
| Whole-snapshot replay | One-use `snapshot_id` receipt journal; accepted IDs are rejected on reuse | Replay on a different host with an unshared journal is not globally detectable |
| Stale source reads hidden inside a fresh envelope | Exporter validates each read's `fetched_at`; runtime separately validates whole-envelope age and State `stale_after` | A compromised trusted exporter could lie about `fetched_at` |
| Future-dated snapshot/read | Exporter and runtime enforce bounded clock skew | Host clock compromise can undermine time-based checks |
| Snapshot tampering after export | Deterministic content digest checked by runtime | Digest is integrity, not cryptographic source authentication |
| Unknown or downgraded source kind | Closed source-kind registry; unknown kinds fail closed | Legitimate new source kinds require an explicit code/schema change |
| Per-fact source spoofing | Exporter assigns `source_kind` from enclosing read receipt | Trust still depends on connector orchestrator correctly classifying the read |
| Missing workflow dependency | Envelope and request declarations must match exactly; runtime read guard rejects undeclared dependencies | Code that consumes JACS outside the guarded library remains a bypass |
| Authority downgrade to recurrence/cache | Consequential required facts need a fresh non-predictive read; runtime authority ranking still applies | Incorrectly classified source evidence remains a trusted-input problem |
| Scheduler representation false positive | RRULE/schedule normalization before comparison | Semantically exotic iCalendar constructs outside normalizer coverage can still drift |
| Scheduler state drift | Enabled/timing/notification/email differences block when sync required | User receipt remains unobservable from scheduler `last_run` alone |
| Local journal misrepresented as canonical JACS | Local records explicitly say canonical writeback pending/not applicable | Canonical reconciliation still requires a separate connected Google-Sheets write/readback |
| Direct `control_hub_agent.py` operational launch | Activation policy forbids it; CI/harness audits supported service/operator entrypoints and requires the generated service to enter through `fleetctl hub-runtime serve` | A developer can still deliberately run the module manually; host procedures must forbid it |
| Wrong runtime state directory | Runtime caller derives snapshot/journals from the actual bound DB parent | A compromised runtime config remains outside this control |
| TOCTOU after preflight | One-use snapshot minimizes stale reuse and preflight occurs immediately before core scan | Source state can change after snapshot generation; keep max age short |
| Compromised exporter/orchestrator | Separation of duties, read receipts, schema, digest, host verification | This package does not cryptographically attest connector results |

## Operational entry enforcement

`config/jacs-host-activation-policy.json` is the activation allowlist. Supported host
service/operator commands must enter the JACS-consuming Control Hub path through
`control_hub_safe_entry.guarded_run_scan`. The package explicitly forbids
`python[3] ops/control_hub_agent.py` as an operational command.

The activation harness verifies:

- `control_hub_agent.py` remains non-executable in the checkout;
- supported wrapper/runtime files do not shell out to `control_hub_agent.py`;
- the generated systemd user service launches `fleetctl hub-runtime serve --config`;
- `fleetctl` keeps strict JACS mode on Control Hub commands.

This is operational enforcement, not a claim that Python imports are blocked. Tests
and safe-entry imports continue to use `control_hub_agent` as a library.

## Dry-run activation plan — do not execute from this PR

The following commands describe the intended host sequence only. They were **not**
run as part of PR #13 validation.

```bash
# 0. Select the reviewed commit exactly; do not substitute a later head.
REVIEWED_SHA=<reviewed-pr-13-head>
STATE_DIR=<bound-runtime-state-dir>
CONFIG=<bound-runtime-config>

# 1. Review-only checkout/worktree preparation.
git fetch origin "$REVIEWED_SHA"
git diff --stat <approved-base> "$REVIEWED_SHA"
git diff <approved-base> "$REVIEWED_SHA"

# 2. Verify the package before any service activation.
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 tests/jacs_host_activation_harness.py
python3 tests/control_hub_non_root_smoke.py

# 3. Connector side: after fresh reads, build a one-use snapshot.
python3 ops/jacs_snapshot_exporter.py \
  --input <fresh-connector-export-spec.json> \
  --output "$STATE_DIR/jacs_preflight.json"

# 4. Host negative proof: use test fixtures/harness only, not production data.
python3 tests/jacs_host_activation_harness.py

# 5. Host positive proof: generate fresh snapshot A, run one guarded scan,
#    confirm replay A refuses, generate fresh snapshot B, run the next scan.
fleetctl hub-runtime scan --config "$CONFIG"

# 6. Only after all evidence is captured and reviewed would service activation
#    be eligible for separate authorization.
fleetctl hub-runtime install-user-service --config "$CONFIG" --no-start
```

The last command uses `--no-start`; actual enabling/starting a service is deployment
and is outside this PR.

## Canonical writeback boundary

There are three distinct persistence domains:

1. `jacs_preflight.json` — one-use host authorization snapshot.
2. `jacs_stale_events.jsonl` / `jacs_snapshot_receipts.jsonl` — local append-only
   provenance and anti-replay evidence.
3. Google Sheets / canonical JACS Evidence, Audit, State, Workflow rows — authoritative
   registry persistence.

Only domain 3 can justify a claim of canonical JACS persistence. A host journal entry
must never be used as evidence that canonical Sheets writeback occurred.

## Promotion evidence required

`WF.JACS.REGISTRY.v1` and `WF.JACS.AUDIT.v1` should remain `DEGRADED` until the
intended host proves all of the following on the exact reviewed revision:

1. full repository CI passes;
2. intended-host non-root smoke passes;
3. activation harness proves all negative cases refuse before `_CORE_RUN_SCAN`;
4. snapshot A succeeds exactly once, replay A refuses, and fresh snapshot B succeeds;
5. generated user service and operator runbook use only policy-approved entrypoints;
6. fresh connector-side exporter reads include registry plus every required
   authoritative provider/bank/scheduler/GitHub/Calendar source for the workflow;
7. snapshot and journals live in the actual private runtime state directory with
   required ownership/modes;
8. local stale/replay journal evidence is reconciled to canonical JACS through a
   separate connected-source write and readback where canonical mutation is needed;
9. no unsupported direct `control_hub_agent.py` operational invocation exists in
   service files, aliases, runbooks, or host procedures;
10. host logs prove refusal occurs before core scan/database mutation for each
    negative fixture;
11. a fresh source reread is performed before each consequential execution rather
    than reusing an accepted snapshot;
12. the resulting canonical JACS Audit records identify the reviewed commit,
    snapshot IDs/digests, host proof, and CI run.

Only after those readbacks are persisted should both workflows move to
`VERIFIED_OPERATIONAL`.
