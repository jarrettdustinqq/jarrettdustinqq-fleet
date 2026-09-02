# Adaptive Operations Governor

## Purpose

The Adaptive Operations Governor (AOG) is a fail-closed policy layer above the
existing JACS registry, Task Multiplexer A (MUXA), primitive scheduler heartbeat,
and bounded executors. It does not replace any of them.

AOG has three responsibilities:

1. rank due work across loops with one deterministic policy;
2. evaluate bounded cadence/lifecycle proposals from heuristics or an untrusted
   reasoning advisor; and
3. emit at most one reversible canary mutation intent after every persistence,
   heartbeat, authority, and readback gate passes.

AOG is not a scheduler, event store, model memory, or actuator. JACS remains the
only canonical state/evidence/audit store. Existing schedulers remain the source
of truth for live task configuration, and existing executors remain the only
components that may perform work.

## Architecture

```text
fixed scheduler heartbeat / source events
                  |
                  v
        canonical JACS snapshot
                  |
                  v
 deterministic AOG policy kernel <--- bounded advisor proposals (untrusted)
                  |
         +--------+---------+
         |                  |
         v                  v
 shadow dispatch plan   one canary intent (only after all gates)
         |                  |
         v                  v
 JACS Evidence/Audit    existing scheduler adapter
                            |
                            v
                 scheduler + JACS exact readback
```

The kernel has no network client and writes no external state. It accepts one
JSON snapshot and emits one deterministic JSON decision envelope. Re-running the
same snapshot produces the same decision and intent identifiers.

## Canonical Loop Registry

The canonical registry lives in the existing JACS workbook. The repository holds
only executable policy and the export schema; it is not a competing registry.

Each live physical or logical loop must resolve these fields from JACS:

- stable loop, objective, scheduler, and parent-loop identifiers;
- trigger and current/minimum/maximum cadence with fixed phase;
- dependencies with current status and freshness;
- exact authority level and whether the current grant is valid;
- per-run call/runtime/cost budget and estimated consumption;
- success evidence and verified-abstention criteria;
- idempotency rule and source-event key;
- lifecycle, allowed adaptation actions, pause rule, and retirement rule;
- benefit, success probability, information gain, branching value, external
  progress probability, employment compatibility, cost, attention, and risk;
- no-change, failure, and deferral streaks;
- last run, next due, deadline, source freshness, evidence references, and open
  contradictions.

Unknown values remain explicit unknowns. They must not be replaced with model
inference merely to make a loop scoreable.

## Journal gate

Production mutation is impossible unless the snapshot proves all of the
following for at least five consecutive independent actual MUXA runs:

- one provider-atomic Audit cohort for all considered logical jobs;
- exact Audit cardinality and idempotency readback;
- unchanged prior-neighbor integrity;
- State projection only after Audit verification;
- final Audit and complete State-row readback; and
- preservation, without reconstruction, of the historical T05 loss.

Any later mismatch resets the streak and forces effective mode to `SHADOW`, even
when `CANARY` was requested. A fail-closed canary request exits with status 3 and
still emits its diagnostic decision envelope.

## Deterministic ranking

Eligibility is evaluated before scoring. A loop is rejected when disabled,
paused/retired/quarantined, stale, contradicted, duplicate, not due, over budget,
missing authority, or blocked by a critical dependency.

Eligible work is ranked by:

- hard obligations and priority class;
- deadline pressure;
- expected realized benefit;
- information gain and external branching value;
- probability of external progress;
- compatibility with employment continuity;
- bounded starvation relief;
- cost, attention burden, and risk penalties.

Capacity is then applied deterministically (`max_dispatches`, `max_heavy`, and
`max_calls`). Starvation relief cannot outrank a hard obligation. If capacity
would starve a hard obligation, AOG emits a critical diagnostic instead of
silently accepting the result.

`WOULD_DISPATCH` is a shadow recommendation. It is not execution evidence.

## Advisor boundary

A reasoning advisor may submit only an allowlisted proposal:

- `SKIP_ONCE`
- `PAUSE`
- `RESUME`
- `SET_CADENCE`

Each proposal must name an existing loop, include evidence, and stay inside that
loop's canonical adaptation bounds. Extra fields are rejected, preventing a
proposal from laundering an objective, permission, phase, or stop-condition
change. The deterministic kernel independently checks deadlines, hard
obligations, recovery state, dependencies, lifecycle, cadence bounds, canary
scope, expiry, and rollback requirements.

The advisor never becomes the source of authority or truth.

## Shadow operation

Run the evaluator from an exact JACS export:

```bash
python3 ops/adaptive_operations_governor.py \
  /path/to/jacs-aog-snapshot.json \
  --pretty \
  --output /path/to/aog-decision.json
```

Print the compiled policy hash for the JACS snapshot precondition:

```bash
python3 ops/adaptive_operations_governor.py --print-policy-hash
```

In shadow mode, AOG emits:

- explicit gate state and fail-closed reasons;
- ranked inclusion and rejection decisions;
- score components;
- accepted/rejected adaptation proposals;
- recent outcome metrics; and
- no mutation intents.

Persist the decision ID, snapshot hash, policy hash, inclusion/rejection reasons,
and outcome comparison to JACS Evidence/Audit. Do not change live scheduler
configuration during shadow operation.

## One-loop canary

After a new five-run MUXA streak is independently verified, a canary still
requires one current authority record binding:

- one exact loop ID;
- an allowlist drawn from the four adaptation actions;
- issue and expiry timestamps;
- exactly one mutation;
- a named verification method; and
- mandatory rollback.

When all gates pass, AOG may emit one intent containing the exact before/after
configuration, a precondition hash, the existing scheduler ID, required
readbacks, and the exact rollback state. The existing scheduler adapter may apply
that intent only after an idempotency preflight. It must then verify live
scheduler configuration, canonical Loop Registry state, Audit durability,
neighbor integrity, and the unchanged fixed heartbeat before success is counted.

The canary stops immediately on any:

- Audit/State mismatch;
- duplicate external effect;
- missed hard deadline;
- stale or contradictory critical state;
- heartbeat loss;
- policy-hash drift;
- unauthorized proposal;
- scheduler/JACS readback mismatch; or
- failure to restore the exact prior configuration.

## Outcomes and metrics

Only these classes count as value:

- `VERIFIED_CONSEQUENCE`
- `VERIFIED_INFORMATION_GAIN`
- `VERIFIED_ABSTENTION`

`NO_MATERIAL_CHANGE`, `BLOCKED`, `FAILED`, and `CONTRADICTED` remain visible but
do not count as progress. Prompts, runs, drafts, plans, artifacts, attempted
calls, and unverified scheduler status never count as value.

Compare baseline and canary on:

- no-material-change execution rate;
- time to detect a material event;
- hard-deadline misses;
- duplicate effects;
- Audit/State mismatches;
- blocked/failure/contradiction rate;
- calls, runtime, cost, and Jarrett attention; and
- verified value outcomes per unit cost.

## Verification

```bash
python3 -m py_compile ops/adaptive_operations_governor.py
python3 -m unittest tests.test_adaptive_operations_governor -v
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

The focused suite covers stale and contradictory state, duplicate source events,
partial Audit/readback gates, executor/dependency failure, missed heartbeats,
deadline pressure, capacity starvation, priority inversion, policy drift,
restart determinism, exact rollback, out-of-bounds cadence, unauthorized advisor
fields, one-loop canary scope, and progress-accounting rules.

## Rollback

Repository rollback is a normal revert of the AOG commit. No scheduler or JACS
state depends on the repository code until an existing adapter is explicitly
configured to consume its output.

For a canary mutation, use the intent's `rollback.restore` object to restore the
exact prior enabled state, lifecycle, cadence, and phase. Verify the scheduler
first, then JACS, then append the rollback evidence. Never infer rollback success
from the mutation call itself.
