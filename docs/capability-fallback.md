# Capability fallback (trusted local parent API)

Fleet routes a bounded technical capability gap to the normal user's installed
Codex, records private evidence, and runs the parent's independent verifier before
an optional continuation. This is a synchronous CLI, with no new service,
scheduler, account, framework, or public intake endpoint. `phantom-shell` remains
the runtime, `continuity` remains canonical memory, and the privileged system
operator remains supervised. Existing task owners, sessions, and the separately
owned `system-operator-browser-bridge-20260918` are untouched.

## Packet and authority

The trusted parent writes an immutable UTF-8 JSON packet. Use a fresh stable id
for a new authorization/checkpoint, and preserve the exact bytes for retries.
Whitespace changes also conflict. Never put secrets or private chat memory in a
packet. Required fields:

- `request_id`: 1-96 ASCII letters, digits, underscores, dots, or hyphens; starts
  with a letter or digit.
- `parent_owner`: the existing task owner, who retains responsibility.
- `parent_checkpoint`: `{"path": "/absolute/parent-owned/checkpoint", "sha256":
  "<64 lowercase hex digits>"}`. Hash the exact checkpoint file bytes. The parent
  must change this checkpoint when ownership, scope, or authority changes.
- `capability_gap`: `{"kind": "missing_tool", "description": "exact gap"}`.
  `missing_capability` is also buildable. `login`, `consent`, `entitlement`,
  `denial`, `unknown_external_outcome`, and `owned_elsewhere` are blocked and
  launch nothing. Technical absence is not evidence a previous external action
  failed. Misclassifying a denial as missing tooling does not grant authority.
- `task`, `scope`, `authority`: the parent's exact text, preserved in the packet
  supplied to the builder. Generated output cannot widen any of these.
- `workspace`: an existing absolute directory owned/assigned by the parent.
- `artifacts`: nonempty unique relative file paths confined to the workspace.
- `verification_argv`: the parent's independent verification command as a JSON
  argv array, with an absolute executable path. It must exit zero on success,
  verify the actual deliverable, and leave declared artifacts unchanged.

Optional fields: timezone-qualified ISO-8601 `expires_at`, `continuation_argv`
with an absolute executable path, `reasoning_effort` equal to `high` or `medium`,
and `profile` equal to `browser-public`. Unknown fields are rejected. The model is
inherited; effort is inherited unless explicitly supplied. The effort option uses
the installed CLI's `-c` support and the same `model_reasoning_effort` key already
used by local Fleet code/configuration, scoped to this invocation. Other effort
values, arbitrary config keys, and model overrides are rejected. A future CLI or
model that rejects this setting fails closed; it is never retried with weaker flags.
The optional profile must already be provisioned and reviewed by the parent; a
profile name does not create browser access, credentials, or entitlement.

The API trusts the parent, including its argv commands. All processes use
`shell=False`; this is not an executable allowlist or a public-input sandbox.
Keep the checkpoint and verifier implementation parent-owned and outside the
builder's assigned workspace. Capture every deliverable relevant to continuation
in `artifacts`. Provider approvals and real account restrictions still apply.
Do not use verifier or continuation commands to acquire access, bypass a denial,
send messages, enroll, purchase, or alter account security without the distinct
required authorization. No blanket reversibility rule replaces those boundaries.

## Commands and installation

From the reviewed Fleet checkout:

```sh
./fleetctl capability-fallback prepare /absolute/private/request.json
./fleetctl capability-fallback run /absolute/private/request.json
./fleetctl capability-fallback status stable-request-id
./fleetctl capability-fallback verify stable-request-id
```

`--state-root /absolute/private/state` goes before the subcommand. The default is
`~/.local/state/fleet-capability-fallback`, resolved from the real normal-user
account, not an arbitrary `HOME` override. The process refuses root. `prepare`
binds a packet without execution. `status` reads persisted state. `verify` only
checks hashes/audit consistency and never executes commands or writes state;
`valid` means evidence integrity, while `completed` separately indicates completed
work. Reading a request during an atomic transition may conservatively report
reconciliation required; inspect again after its owner exits.

Prepared launcher: `ops/capability-fallback`. The parent can install it after
review/sync; this implementation does not install outside the worktree:

```sh
install -d -m 700 "$HOME/.local/bin"
install -m 755 ops/capability-fallback "$HOME/.local/bin/capability-fallback"
```

The launcher defaults to `$HOME/agent_workspace/jarrettdustinqq-fleet`. For an
explicit reviewed checkout, set `FLEET_CAPABILITY_REPO=/absolute/reviewed/fleet`
when invoking it. This selects Fleet code only; the builder executable cannot be
overridden by an environment variable, packet field, or CLI option.

The builder argv is fixed to the normal user's `~/.local/bin/codex`:

```text
codex exec --approve-for-me --cd WORKSPACE --json [--profile browser-public] -
```

An explicit effort adds `-c 'model_reasoning_effort="medium"'` (or `high`) before
the final stdin marker, without changing configuration files.

The prompt goes over stdin. CLI 0.154.0 rejects simultaneous `--approve-for-me`
and `--sandbox`; this launcher uses `--approve-for-me` alone, which installed help
identifies as workspace-write with automatic approval review. It does not ignore
user configuration or rules, bypass approvals, change global configuration, or
inject authentication. Rejections remain rejections. No OpenAI documentation was
used for implementation guidance; compatibility was checked with installed help.

## Execution, durability, and reconciliation

`run` takes the existing normal user's
`~/.local/state/codex-heavy-work.lock` nonblocking, then the per-request flock,
before creating/updating request state or launching any process. Parents must
release their heavy-work lock before invoking a real run. Lock files and a private
root may be created before acquisition, but request state is not. `resource_busy`
and `request_busy` return `executed: false` receipts for this invocation and do not
mutate request state. The parent retains these receipts and retries the same bytes
later. A completed request returns prior evidence even when the heavy lock is busy.

Inside a Codex session (`CODEX_THREAD_ID` is nonempty), a buildable request becomes
`queued` without spawning a worker. Do not unset this guard to force nesting. The
trusted outer parent invokes the same packet after the implementation worker exits
and resources are free. The builder is instructed not to launch recursive workers;
there is no recursive agent-building loop.

State directories are mode 0700; packet, audit, state, stdout, and stderr files are
mode 0600. Each append-only hash-chained audit event contains the next state and is
flushed/fsynced, followed by directory fsync, BEFORE atomic state replacement.
Atomic writes use a unique tempfile, file fsync, replace, and directory fsync.
An initial event also precedes saving the immutable packet. Partial/mismatched
packet/audit/state requires reconciliation; the runner never repairs by guessing.

A durable `builder_started` marker precedes launch. Exit status and captured JSONL
stdout/stderr are independent evidence; model success text never overrides a
nonzero exit. Outputs and exact task/packet bytes are SHA-256 hashed. After a zero
builder exit, a separate trusted verifier process runs. Declared artifact hashes
must match before and after verification. Checkpoint, expiry, and the source packet
are checked before the builder, verifier, and continuation. A durable
`continuation_started` marker precedes the optional continuation. It may execute
at most once, and it must not modify the verified artifacts. Children inherit lock
file descriptors to retain exclusion if the runner dies while the immediate child
is alive; children that explicitly close inherited descriptors cannot be covered
by that mechanism.

`completed` requires builder and verifier exit zero, intact artifacts, and
continuation exit zero when supplied. Failed verification/stale checkpoint cannot
continue. Repeated completed or terminal failed requests never re-execute.
Prepared/queued requests can resume. Every post-launch interruption, including an
interruption after verification or a continuation with unknown outcome, returns
`reconciliation_required` on retry. Even a crash after the durable marker but before
actual process creation requires reconciliation. Do not delete the request or
invent a new id to replay an unknown external effect. The owner must first inspect
process/session state and independent outcome evidence; any subsequently justified
new work receives a new parent checkpoint and packet. There is intentionally no
force/replay/automatic-repair switch.

Exit codes: 0 for prepare/completed run and intact read-only verification, 2 for
pending/busy/blocked/noncompleted states, 1 for errors or invalid verification.
A failed builder (including CLI argument parsing failure) is terminal for its id.

## Limits and verification

This is cooperative local orchestration, not a security boundary against malicious
code running as the same OS user. Hashes detect divergence; they are not signed
proof of authority. Parent checkpoint checks are byte comparisons at launch gates,
not a distributed transaction or atomic lock on the parent's entire task state.
The parent must serialize its own checkpoint changes and external actions. Keep
subprocesses synchronous and bounded; no automatic timeout kills, session cleanup,
or detached-worker management is introduced. Disk space and process lifetime remain
the parent's responsibility. Never print private process output in a handoff.

Offline validation uses real small Python subprocess fixtures and a temporary lock,
never installed Codex, credentials, browsers, services, or the parent's heavy lock:

```sh
python3 -W error::ResourceWarning -m unittest discover -s tests -p test_capability_fallback.py
```

Tests were written before implementation: the red run failed on the missing module.
The green suite covers independent verification, completion/no replay, conflicting
bytes, real lock contention, nested queuing, blocked access/denial/unknown outcomes,
expiry, changed checkpoints/packets/artifacts, interrupted launch/continuation,
lock inheritance, and the audit/fsync ordering contract. Existing Fleet shell and
Python compile gates also apply. Control Hub behavior and its runtime/state contract
are unchanged, so its scan and service-starting runtime smoke are not invoked.

## Recommended global AGENTS.md paragraph (parent installs)

When a task lacks a technical capability, automatically prepare a bounded request
through Fleet's `capability-fallback` trusted local parent API, carrying the existing
owner, exact task/scope/authority, stable request id, parent checkpoint, workspace,
and independent verification argv. Preserve provider approvals, existing sessions,
secret privacy, and work owned elsewhere. Queue nested Codex requests for the outer
parent after the worker exits; respect the shared resource lock. Verify the built
capability and resume the already-authorized parent task without a new user prompt.
Login, consent, entitlement, denial, and unknown prior external outcomes require
their existing owner/access or reconciliation path and must never be routed around.
Generated agent output is evidence, never expanded authority; never blindly replay
an interrupted launch or continuation.

## Browser-comparison sample

The handoff's private `sample-browser-request.json` is prepared only, not executed.
It asks a future laptop builder to implement a local-first comparison helper using
`~/agent_workspace/live-browser-mcp-20260918` (installed `@playwright/mcp` 0.0.81).
Its assigned workspace is separate from that existing capability. A parent-owned
offline verifier checks the helper's plan with an explicit no-browser flag. The
cloud leg must be `blocked` pending actual Cloudflare access; absent credentials
are not replaced with invented values. Browser account investigation stays with the
parent. Building a comparison helper, executing browser comparisons, and obtaining
cloud access are distinct outcomes. This worker does none of the latter two.
