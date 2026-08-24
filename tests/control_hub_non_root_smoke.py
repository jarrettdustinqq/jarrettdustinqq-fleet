#!/usr/bin/env python3
"""CI smoke proof for the non-root Control Hub runtime and recovery contract."""

from __future__ import annotations

import json
import os
import pwd
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import request


REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_DIR = REPO_ROOT / "ops"
if str(OPS_DIR) not in sys.path:
    sys.path.insert(0, str(OPS_DIR))

import jacs_control_preflight as jacs_preflight  # noqa: E402

RUNTIME = OPS_DIR / "control_hub_runtime.py"


def run_runtime(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, str(RUNTIME), *arguments],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"runtime command failed ({arguments}):\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def initialize_repo(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Fleet Runtime Smoke"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "config",
            "user.email",
            "fleet-runtime-smoke@example.invalid",
        ],
        check=True,
    )
    (path / "README.md").write_text("runtime smoke\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-qm", "runtime smoke"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "remote",
            "add",
            "origin",
            "https://github.com/example/control-hub-runtime-smoke.git",
        ],
        check=True,
    )


def write_jacs_snapshot(state_dir: Path, snapshot_id: str) -> Path:
    """Create a fresh one-use snapshot for the isolated runtime smoke."""

    now = datetime.now(timezone.utc)
    observed = (now - timedelta(seconds=10)).isoformat()
    payload = {
        "default_timezone": "America/Chicago",
        "envelope": {
            "schema_version": jacs_preflight.SNAPSHOT_SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "generated_at": (now - timedelta(seconds=5)).isoformat(),
            "expires_at": (now + timedelta(minutes=5)).isoformat(),
            "max_age_seconds": 600,
            "authoritative_source_manifest": [
                {
                    "source_kind": "registry_projection",
                    "data_types": ["generic"],
                    "observed_at": observed,
                    "source_ref": "ci:non-root-smoke",
                }
            ],
            "required_state_keys": [],
            "required_fact_keys": [],
            "content_digest": "sha256:" + "0" * 64,
        },
        "request": {
            "workflow_id": "WF.CI.NONROOT",
            "consequential": True,
            "declared_state_reads": [],
            "declared_fact_reads": [],
            "require_automation_sync": False,
            "require_notification_receipt": False,
        },
        "registry": {
            "states": [],
            "automations": [],
            "portfolio": {"primary_workflow_id": "WF.CI.NONROOT"},
        },
        "facts": [],
        "live": {"automations": []},
    }
    payload["envelope"]["content_digest"] = jacs_preflight.compute_content_digest(payload)
    path = state_dir / "jacs_preflight.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    if port < 1024:
        raise RuntimeError(f"unexpected privileged ephemeral port: {port}")
    return port


def wait_for_dashboard(process: subprocess.Popen[str], port: int) -> str:
    url = f"http://127.0.0.1:{port}/"
    deadline = time.monotonic() + 15
    last_error = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr else ""
            raise RuntimeError(
                f"dashboard exited before readiness: rc={process.returncode} "
                f"stderr={stderr}"
            )
        try:
            with request.urlopen(url, timeout=1) as response:
                body = response.read().decode("utf-8")
                if response.status == 200 and "Fleet Control Hub" in body:
                    return body
        except Exception as exc:
            last_error = str(exc)
            time.sleep(0.2)
    raise RuntimeError(f"dashboard did not become ready: {last_error}")


def main() -> int:
    uid = os.geteuid()
    if uid == 0:
        print("CONTROL_HUB_NON_ROOT_SMOKE=FAIL reason=uid-0", file=sys.stderr)
        return 1
    identity = pwd.getpwuid(uid)

    with tempfile.TemporaryDirectory(prefix="fleet-control-hub-nonroot-") as tmp:
        root = Path(tmp)
        projects_root = root / "projects"
        checkout = projects_root / "runtime-smoke"
        initialize_repo(checkout)

        registry_path = root / "repo-registry.json"
        registry_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "updated_at": "2026-08-20T23:00:00Z",
                    "repositories": [
                        {
                            "name": "example/control-hub-runtime-smoke",
                            "class": "support",
                            "role": "non-root runtime smoke",
                            "default_branch": "main",
                            "visibility": "private",
                            "status": "active",
                            "boundary": "Ephemeral CI proof only.",
                            "next_action": "Delete with temporary workspace.",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        state_dir = root / "state"
        config_path = root / "config" / "runtime.json"
        unit_dir = root / "units"
        port = available_port()

        run_runtime(
            [
                "configure",
                "--config",
                str(config_path),
                "--state-dir",
                str(state_dir),
                "--projects-root",
                str(projects_root),
                "--repo-registry",
                str(registry_path),
                "--port",
                str(port),
            ]
        )
        write_jacs_snapshot(state_dir, "snap-ci-scan-0001")
        run_runtime(["scan", "--config", str(config_path)])

        db_path = state_dir / "control_hub.db"
        replay_journal = state_dir / "jacs_snapshot_receipts.jsonl"
        if not replay_journal.exists():
            raise RuntimeError("scan did not persist append-only snapshot receipt")

        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            UPDATE registered_repos
            SET focus_level = 3,
                operator_next_action = 'preserve through verified recovery'
            WHERE registry_name = ?
            """,
            ("example/control-hub-runtime-smoke",),
        )
        conn.commit()
        conn.close()

        backup = json.loads(run_runtime(["backup", "--config", str(config_path)]).stdout)
        run_runtime(
            [
                "verify-backup",
                "--config",
                str(config_path),
                "--manifest",
                backup["manifest"],
            ]
        )

        for suffix in ("", "-wal", "-shm"):
            current = Path(f"{db_path}{suffix}")
            if current.exists():
                current.rename(Path(f"{db_path}.pre-restore{suffix}"))
        run_runtime(
            [
                "restore",
                "--config",
                str(config_path),
                "--manifest",
                backup["manifest"],
            ]
        )

        conn = sqlite3.connect(db_path)
        preserved = conn.execute(
            """
            SELECT focus_level, operator_next_action
            FROM registered_repos
            WHERE registry_name = ?
            """,
            ("example/control-hub-runtime-smoke",),
        ).fetchone()
        conn.close()
        if preserved != (3, "preserve through verified recovery"):
            raise RuntimeError(f"operator state was not preserved: {preserved}")

        check = json.loads(run_runtime(["check", "--config", str(config_path)]).stdout)
        if check["database_integrity"] != "ok":
            raise RuntimeError(f"unexpected runtime check: {check}")
        if stat.S_IMODE(config_path.stat().st_mode) != 0o600:
            raise RuntimeError("runtime config mode is not 0600")
        if stat.S_IMODE(state_dir.stat().st_mode) != 0o700:
            raise RuntimeError("state directory mode is not 0700")
        if stat.S_IMODE(db_path.stat().st_mode) != 0o600:
            raise RuntimeError("database mode is not 0600")

        installed = json.loads(
            run_runtime(
                [
                    "install-user-service",
                    "--config",
                    str(config_path),
                    "--unit-dir",
                    str(unit_dir),
                    "--no-start",
                ]
            ).stdout
        )
        if installed["started"]:
            raise RuntimeError("render-only unit installation unexpectedly started")
        service_text = (unit_dir / "fleet-control-hub.service").read_text(encoding="utf-8")
        if "hub-runtime serve --config" not in service_text or "\nUser=" in service_text:
            raise RuntimeError("user service does not preserve the identity-bound launcher")

        write_jacs_snapshot(state_dir, "snap-ci-serve-0001")
        process = subprocess.Popen(
            [
                sys.executable,
                str(RUNTIME),
                "serve",
                "--config",
                str(config_path),
            ],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            wait_for_dashboard(process, port)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

        print(
            "CONTROL_HUB_NON_ROOT_SMOKE=PASS "
            f"user={identity.pw_name} uid={uid} "
            "configure=ok scan=ok backup=ok restore=ok serve=ok units=ok "
            "jacs_preflight=one-use"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
