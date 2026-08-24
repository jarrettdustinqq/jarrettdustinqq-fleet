# JACS preflight executable audit

This audit separates **consequential execution** from **JACS-dependent execution**. The JACS freshness/authority gate is mandatory for paths that consume the JACS Structured Control Registry before Control Hub execution. It is not an authorization substitute for unrelated host, credential, repository, Linear, or installer actions.

## Executable-mode repository surfaces

The branch tree marks the following repository files executable (`100755`).

| Surface | Consequential behavior | Consumes JACS control evidence? | JACS binding |
| --- | --- | --- | --- |
| `fleetctl` | Multiplexes all supported Fleet operator commands | **Yes**, for `mission-control`, `hub-scan`, `hub-serve`, and `hub-runtime scan/serve` | **Bound**. `enable_jacs_preflight` establishes strict mode and private snapshot/journal defaults before those control-plane commands. |
| `bootstrap.sh` | Creates SSH material and clones/updates repositories | No | Not applicable. Requires its own operator/repository authority; JACS evidence must not be treated as permission to create keys or mutate repos. |
| `install_nix.sh` | Installs Nix in user mode | No | Not applicable; host-install authority is separate. |
| `healthcheck.sh` | Reads local health/state | No | Not applicable/read-only. |
| `ops/remote_access_agent.sh` | Manages/checks SSH and remote-access prerequisites | No | Not applicable; credential/remote-access authority is separate. |
| `ops/vps_discovery_agent.py` | Discovers likely VPS targets | No | Not applicable; observation only. |
| `ops/install_chat_work_agent_timer.sh` | Installs/manages a user timer | No | Not applicable; systemd mutation is separately authorized. |
| `ops/install_venture_agent_timer.sh` | Installs/manages a user timer | No | Not applicable; systemd mutation is separately authorized. |
| `ops/seed_linear_issues.sh` | Can create/update external Linear work items | No | Not applicable. External issue mutation requires separate explicit authority; a passing JACS preflight must never imply that authority. |

## Python control-plane entry modules (`100644`)

These are invoked through `fleetctl` or other Python entry modules rather than executable mode, but they were audited because direct `python <file>` invocation is possible.

| Module | Audit result |
| --- | --- |
| `ops/control_hub_safe_entry.py` | Single JACS authorization point is `guarded_run_scan`, immediately before the core scanner. Direct script execution enables strict JACS mode. |
| `ops/mission_control_agent.py` | Direct invocation binds the same private JACS snapshot/stale-journal defaults before launching safe entry. |
| `ops/control_hub_runtime.py` | `scan`/`serve` enter `control_hub_safe_entry`; safe entry recognizes the runtime caller and requires JACS. `check`, `backup`, `verify-backup`, `restore`, `migrate`, and unit rendering do not consume JACS State/facts and remain governed by the runtime's ownership/integrity contract. |
| `ops/control_hub_agent.py` | Internal implementation, mode `100644`. Supported Fleet and systemd paths do **not** execute it directly; they import it through safe entry. Manual `python ops/control_hub_agent.py` is an unsupported developer path and must not be used as a host-activation command. |
| `ops/chat_work_agent.py` | Generates local workstream inventory; no JACS State/fact consumption. |
| `ops/venture_autonomy_agent.py` | Generates repository/action analysis and optional checks; no JACS State/fact consumption. |
| `ops/jacs_control_preflight.py` | Compatibility re-export only; cannot preserve the former rank-zero implementation. |
| `ops/jacs_snapshot_boundary.py` | Hardened preflight implementation; no external scheduler/provider mutation and no canonical Google-Sheets writeback. |

## Supported-host invariant

A host is not eligible for `WF.JACS.REGISTRY.v1` / `WF.JACS.AUDIT.v1 = VERIFIED_OPERATIONAL` unless all supported Control Hub service and operator commands reach `guarded_run_scan` with strict preflight enabled, a fresh schema-v2 snapshot in the runtime's private state directory, and writable append-only stale/replay journals. A manual direct launch of `control_hub_agent.py` is outside the supported host contract and is a promotion blocker if operational procedures or service units permit it.

## Authority boundary

A passing JACS preflight proves only that the declared JACS dependencies were fresh, complete, source-authority-consistent, and non-replayed for that execution. It does **not** authorize money movement, external communication, credential changes, repository merges/deployments, Linear mutations, scheduler mutation, or agreement acceptance.
