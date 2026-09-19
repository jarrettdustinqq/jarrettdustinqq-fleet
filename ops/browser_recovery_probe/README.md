# Public browser recovery probe

Owner: `root/browser-comparison-20260919`.
Request: `browser-probe-076fbc07edd4470bb6ed1e69`.
Parent checkpoint: `/home/jarrettdustinqq/agent_workspace/capability-fallback-20260919/live-parent-checkpoint.json`
SHA256: `d870bbda7aec34c4d760889473a9ead60dcdba112e9b05ff0f27a1be06b76688`.
These identifiers preserve provenance; generated files grant no authority.

Cleanup repair request: `browser-repair-784cff10d9e0cf2374091802`.
Owner remains `root/browser-comparison-20260919`.
Repair parent checkpoint: `/home/jarrettdustinqq/agent_workspace/capability-fallback-20260919/repair-parent-checkpoint.json`
SHA256: `3854ebb81b2dbbbe1827764457f45b56f3b4c3bf0c600204326c8a48fbb5f56b`.
This repair follows the parent's reconciled cleanup failure. Historical evidence
and original source commit `9d16c11` remain unchanged. Generated output is evidence,
never authority; the parent owns verification and live continuation.

The stdlib supervisor measures recovery after killing one CDP client while its
owned Chromium remains alive. This is client transport loss, not browser crash
recovery. Only the exact public Discovery Workbench URL is navigated. Cloud stays
`blocked/access_not_established`, with `attempted:false`; no cloud comparison,
credentials, account changes, installs, or external messages are attempted.

```
python3 -I browser_probe.py plan
python3 -I browser_probe.py run --output /absolute/existing/private/directory
```

The parent runs the live command once after independent verification. The builder
runs only offline subprocess cleanup regression tests for this repair. Run the live
probe as an ordinary user with Chromium sandbox support.
Output must already exist, be owned by the current user, and have no group/other
permissions (normally mode 0700). Existing probe evidence is refused: reconcile
unknown previous outcomes with the owner before another run.

Dependencies are `/usr/bin/chromium`, `/usr/bin/node`, and existing Playwright at
`/home/jarrettdustinqq/agent_workspace/live-browser-mcp-20260918/node_modules/playwright`.
No sandbox bypass or dependency installation is provided. The supervisor creates
one unique temporary profile and starts one Chromium instance with a loopback,
automatically allocated debugging port. Existing profiles and sessions are untouched.

Client A reuses the default context/page, requires HTTP 200, the exact URL and title,
and records a random window/sessionStorage nonce, target ID, time origin, endpoint,
and screenshot. It stays alive until the supervisor sends SIGKILL and observes -9.
A separate client B finds that target and reads the session evidence before any
navigation or page mutation. It captures the second screenshot and exits without
closing browser objects. The supervisor compares browser PID start times and
session evidence, then cleans up its owned processes in finally. Chromium starts
in a fresh session/group; `recovery.browser_pgid` and `recovery.profile_path` record
ownership alongside the launch PID/start time. Cleanup finds non-zombie members
of that launch session and exact unique `--user-data-dir` matches (including a
profile process that left the session), retaining observed PID/start identities.
Signals use Linux pidfds after start-time validation to avoid signaling a reused
PID. No global process-name matching or unrelated session termination is used.
Owned processes receive SIGTERM, then SIGKILL after one second, with a five-second
process cleanup deadline. Cleanup waits/reaps the direct browser, verifies no
owned live descendants remain, and checks endpoint refusal. Only after the process
checks pass does it remove the unique profile; failure retains it for reconciliation.
`descendants_exited:true` and `profile_removed:true` require observed completion.
Uninspectable process state fails cleanup rather than asserting success. Successful
startup emits `browser_started.status=passed` independently of unfinished recovery.

```
python3 -I test_cleanup.py
```

These serial offline tests use Python subprocesses only: a group descendant and
an escaped profile descendant ignore SIGTERM while writing; cleanup must stop
them before profile removal and leave an unrelated process alive. A deadline test
requires truthful failure and profile retention. No browser or external calls run.

Startup, each CDP connection, and navigation have 15-second deadlines. A 78-second
supervisor alarm reserves time for bounded process termination and endpoint checks
within the 90-second run budget under a responsive OS/filesystem. Forced supervisor
SIGKILL or an unresponsive kernel/filesystem cannot guarantee cleanup or deadlines.
The inherited parent lock is neither acquired nor explicitly closed here; work is
serial and no workers are detached.

`result.json` and timestamped `events.jsonl` contain observations; `client-a.json`
and `client-b.json` contain checkpoints (or explicit failure/skipped evidence).
Successful runs also produce `before.png` and `after.png`. Failures do not fabricate
screenshots, navigation values, checkpoints, or timings. Browser/client stderr is
retained separately. Exit zero requires navigation, same-session recovery, and
cleanup to pass; all other outcomes return nonzero. Invalid output directories
are rejected with a JSON receipt before starting anything.
