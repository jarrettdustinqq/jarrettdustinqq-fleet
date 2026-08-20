#!/usr/bin/env python3
"""Non-root runtime, state, backup, and user-service contract for Control Hub."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import pwd
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

import control_hub_agent as hub
import control_hub_safe_entry as safe_entry


CONFIG_SCHEMA_VERSION = 1
BACKUP_MANIFEST_SCHEMA_VERSION = 1
MAX_CONFIG_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
SERVICE_NAME = "fleet-control-hub.service"
BACKUP_SERVICE_NAME = "fleet-control-hub-backup.service"
BACKUP_TIMER_NAME = "fleet-control-hub-backup.timer"
UNIT_NAMES = (SERVICE_NAME, BACKUP_SERVICE_NAME, BACKUP_TIMER_NAME)


class RuntimeContractError(RuntimeError):
    """Raised when the explicit non-root runtime boundary is not satisfied."""


@dataclass(frozen=True)
class RuntimeIdentity:
    uid: int
    gid: int
    user: str
    home: Path


@dataclass(frozen=True)
class RuntimeConfig:
    identity: RuntimeIdentity
    state_dir: Path
    db_path: Path
    backups_dir: Path
    chat_work_json: Path
    venture_report_json: Path
    projects_roots: tuple[Path, ...]
    repo_registry: Path
    host: str
    port: int
    window_tracking: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "identity": {
                "uid": self.identity.uid,
                "gid": self.identity.gid,
                "user": self.identity.user,
                "home": str(self.identity.home),
            },
            "state_dir": str(self.state_dir),
            "db_path": str(self.db_path),
            "backups_dir": str(self.backups_dir),
            "chat_work_json": str(self.chat_work_json),
            "venture_report_json": str(self.venture_report_json),
            "projects_roots": [str(path) for path in self.projects_roots],
            "repo_registry": str(self.repo_registry),
            "host": self.host,
            "port": self.port,
            "window_tracking": self.window_tracking,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "RuntimeConfig":
        if not isinstance(raw, dict):
            raise RuntimeContractError("runtime config must be a JSON object")
        required = {
            "schema_version",
            "identity",
            "state_dir",
            "db_path",
            "backups_dir",
            "chat_work_json",
            "venture_report_json",
            "projects_roots",
            "repo_registry",
            "host",
            "port",
            "window_tracking",
        }
        if set(raw) != required:
            missing = sorted(required - set(raw))
            extra = sorted(set(raw) - required)
            raise RuntimeContractError(
                f"runtime config keys mismatch: missing={missing} extra={extra}"
            )
        if raw["schema_version"] != CONFIG_SCHEMA_VERSION:
            raise RuntimeContractError(
                f"unsupported runtime config schema: {raw['schema_version']!r}"
            )

        identity_raw = raw["identity"]
        identity_keys = {"uid", "gid", "user", "home"}
        if not isinstance(identity_raw, dict) or set(identity_raw) != identity_keys:
            raise RuntimeContractError("runtime identity must contain uid/gid/user/home")
        if (
            isinstance(identity_raw["uid"], bool)
            or not isinstance(identity_raw["uid"], int)
            or isinstance(identity_raw["gid"], bool)
            or not isinstance(identity_raw["gid"], int)
            or not isinstance(identity_raw["user"], str)
            or not identity_raw["user"]
            or not isinstance(identity_raw["home"], str)
        ):
            raise RuntimeContractError("runtime identity fields have invalid types")
        identity = RuntimeIdentity(
            uid=identity_raw["uid"],
            gid=identity_raw["gid"],
            user=identity_raw["user"],
            home=require_absolute_path(identity_raw["home"], "identity.home"),
        )

        path_fields = {}
        for field in (
            "state_dir",
            "db_path",
            "backups_dir",
            "chat_work_json",
            "venture_report_json",
            "repo_registry",
        ):
            path_fields[field] = require_absolute_path(raw[field], field)

        roots_raw = raw["projects_roots"]
        if (
            not isinstance(roots_raw, list)
            or not roots_raw
            or any(not isinstance(item, str) for item in roots_raw)
        ):
            raise RuntimeContractError("projects_roots must be a non-empty string list")
        roots = tuple(
            require_absolute_path(item, "projects_roots") for item in roots_raw
        )
        try:
            normalized_roots = hub.normalize_projects_roots(roots[0], roots[1:])
        except hub.RepoDiscoveryError as exc:
            raise RuntimeContractError(str(exc)) from exc
        if normalized_roots != roots:
            raise RuntimeContractError(
                "projects_roots must already be absolute, unique, and normalized"
            )

        state_dir = path_fields["state_dir"]
        expected_paths = {
            "db_path": state_dir / "control_hub.db",
            "backups_dir": state_dir / "backups",
            "chat_work_json": state_dir / "chat_work_brief.json",
            "venture_report_json": state_dir / "venture_autonomy_report.json",
        }
        for field, expected in expected_paths.items():
            if path_fields[field] != expected:
                raise RuntimeContractError(
                    f"{field} must be the authoritative path {expected}"
                )

        host = raw["host"]
        if host not in {"127.0.0.1", "::1"}:
            raise RuntimeContractError("runtime host must be loopback (127.0.0.1 or ::1)")
        port = raw["port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            raise RuntimeContractError("runtime port must be an unprivileged port (1024-65535)")
        if not isinstance(raw["window_tracking"], bool):
            raise RuntimeContractError("window_tracking must be boolean")

        return cls(
            identity=identity,
            state_dir=state_dir,
            db_path=path_fields["db_path"],
            backups_dir=path_fields["backups_dir"],
            chat_work_json=path_fields["chat_work_json"],
            venture_report_json=path_fields["venture_report_json"],
            projects_roots=roots,
            repo_registry=path_fields["repo_registry"],
            host=host,
            port=port,
            window_tracking=raw["window_tracking"],
        )


def lexical_absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def require_absolute_path(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise RuntimeContractError(f"{field} must be a non-empty absolute path")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise RuntimeContractError(f"{field} must be absolute: {value}")
    normalized = lexical_absolute_path(candidate)
    if str(normalized) != value:
        raise RuntimeContractError(f"{field} must be lexically normalized: {value}")
    return normalized


def current_identity() -> RuntimeIdentity:
    uid = os.geteuid()
    gid = os.getegid()
    try:
        record = pwd.getpwuid(uid)
    except KeyError as exc:
        raise RuntimeContractError(f"no passwd entry for effective uid {uid}") from exc
    return RuntimeIdentity(
        uid=uid,
        gid=gid,
        user=record.pw_name,
        home=lexical_absolute_path(Path(record.pw_dir)),
    )


def require_non_root(identity: RuntimeIdentity) -> None:
    if identity.uid == 0:
        raise RuntimeContractError(
            "Control Hub runtime management refuses uid 0; run as the documented "
            "non-root service/operator identity"
        )


def xdg_base(
    identity: RuntimeIdentity,
    variable: str,
    fallback: Path,
) -> Path:
    configured = os.environ.get(variable)
    if not configured:
        return fallback
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        raise RuntimeContractError(f"{variable} must be absolute when set")
    return lexical_absolute_path(candidate)


def default_state_dir(identity: RuntimeIdentity) -> Path:
    configured = os.environ.get("FLEET_CONTROL_HUB_STATE_DIR")
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            raise RuntimeContractError(
                "FLEET_CONTROL_HUB_STATE_DIR must be absolute when set"
            )
        return lexical_absolute_path(candidate)
    data_home = xdg_base(
        identity,
        "XDG_DATA_HOME",
        identity.home / ".local" / "share",
    )
    return data_home / hub.APP_NAME


def default_config_path(identity: RuntimeIdentity) -> Path:
    config_home = xdg_base(
        identity,
        "XDG_CONFIG_HOME",
        identity.home / ".config",
    )
    return config_home / hub.APP_NAME / "runtime.json"


def default_unit_dir(identity: RuntimeIdentity) -> Path:
    config_home = xdg_base(
        identity,
        "XDG_CONFIG_HOME",
        identity.home / ".config",
    )
    return config_home / "systemd" / "user"


def resolve_cli_path(value: Path | None, default: Path) -> Path:
    if value is None:
        return lexical_absolute_path(default)
    return lexical_absolute_path(value)


def mode_bits(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def ensure_private_directory(path: Path, identity: RuntimeIdentity) -> None:
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeContractError(f"private directory must not be a symlink: {path}")
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeContractError(f"private directory is not a directory: {path}")
        if info.st_uid != identity.uid:
            raise RuntimeContractError(
                f"private directory owner mismatch: {path} uid={info.st_uid}, "
                f"expected={identity.uid}"
            )
    else:
        path.mkdir(parents=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError as exc:
        raise RuntimeContractError(
            f"unable to enforce mode 0700 on {path}: {exc}"
        ) from exc


def assert_private_directory(path: Path, identity: RuntimeIdentity) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeContractError(f"private directory is missing: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeContractError(f"private directory is unsafe: {path}")
    if info.st_uid != identity.uid:
        raise RuntimeContractError(
            f"private directory owner mismatch: {path} uid={info.st_uid}, "
            f"expected={identity.uid}"
        )
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeContractError(
            f"private directory permissions are too broad: {path} "
            f"mode={stat.S_IMODE(info.st_mode):04o}; expected 0700"
        )


def assert_private_file(
    path: Path,
    identity: RuntimeIdentity,
    *,
    required: bool = True,
) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if required:
            raise RuntimeContractError(f"private file is missing: {path}")
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeContractError(f"private file is unsafe: {path}")
    if info.st_uid != identity.uid:
        raise RuntimeContractError(
            f"private file owner mismatch: {path} uid={info.st_uid}, "
            f"expected={identity.uid}"
        )
    permissions = stat.S_IMODE(info.st_mode)
    if permissions & 0o077:
        raise RuntimeContractError(
            f"private file permissions are too broad: {path} "
            f"mode={permissions:04o}; expected 0600"
        )
    if permissions & 0o600 != 0o600:
        raise RuntimeContractError(
            f"private file must be owner-readable and owner-writable: {path} "
            f"mode={permissions:04o}; expected 0600"
        )
    return True


def assert_owned_regular_file(
    path: Path,
    identity: RuntimeIdentity,
    *,
    required: bool = False,
) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if required:
            raise RuntimeContractError(f"owned file is missing: {path}")
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeContractError(f"owned file is unsafe: {path}")
    if info.st_uid != identity.uid:
        raise RuntimeContractError(
            f"owned file owner mismatch: {path} uid={info.st_uid}, "
            f"expected={identity.uid}"
        )
    return True


def tighten_owned_private_file(path: Path, identity: RuntimeIdentity) -> None:
    if not (path.exists() or path.is_symlink()):
        return
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeContractError(f"private file is unsafe: {path}")
    if info.st_uid != identity.uid:
        raise RuntimeContractError(
            f"private file owner mismatch: {path} uid={info.st_uid}, "
            f"expected={identity.uid}"
        )
    path.chmod(0o600)


def assert_unit_directory(path: Path, identity: RuntimeIdentity) -> None:
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeContractError(f"systemd user unit directory is unsafe: {path}")
        if info.st_uid != identity.uid:
            raise RuntimeContractError(
                f"systemd user unit directory owner mismatch: {path}"
            )
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise RuntimeContractError(
                f"systemd user unit directory is group/world writable: {path}"
            )
        return
    path.mkdir(parents=True, mode=0o700)


def atomic_write(path: Path, content: str, mode: int) -> None:
    fd = -1
    tmp_path: Path | None = None
    try:
        fd, tmp_text = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        tmp_path = Path(tmp_text)
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
        path.chmod(mode)
    except OSError as exc:
        raise RuntimeContractError(f"unable to write {path}: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp_path is not None:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()


def read_bounded_json(path: Path, max_bytes: int) -> Any:
    try:
        if path.stat().st_size > max_bytes:
            raise RuntimeContractError(
                f"JSON file exceeds {max_bytes} bytes: {path}"
            )
        return json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise RuntimeContractError(f"JSON file is not UTF-8: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeContractError(f"invalid JSON in {path}: {exc}") from exc
    except OSError as exc:
        raise RuntimeContractError(f"unable to read {path}: {exc}") from exc


def prepare_runtime_paths(config: RuntimeConfig, identity: RuntimeIdentity) -> None:
    ensure_private_directory(config.state_dir, identity)
    ensure_private_directory(config.backups_dir, identity)
    for path in (
        config.db_path,
        Path(f"{config.db_path}-wal"),
        Path(f"{config.db_path}-shm"),
        config.chat_work_json,
        config.venture_report_json,
    ):
        tighten_owned_private_file(path, identity)


def validate_identity(config: RuntimeConfig, identity: RuntimeIdentity) -> None:
    if config.identity != identity:
        raise RuntimeContractError(
            "runtime identity mismatch: config binds "
            f"{config.identity.user} uid={config.identity.uid} gid={config.identity.gid}, "
            f"current is {identity.user} uid={identity.uid} gid={identity.gid}"
        )


def load_runtime_config(
    config_path: Path,
    identity: RuntimeIdentity,
) -> RuntimeConfig:
    assert_private_file(config_path, identity)
    config = RuntimeConfig.from_dict(read_bounded_json(config_path, MAX_CONFIG_BYTES))
    validate_identity(config, identity)
    assert_private_directory(config.state_dir, identity)
    assert_private_directory(config.backups_dir, identity)
    for path in (
        config.db_path,
        Path(f"{config.db_path}-wal"),
        Path(f"{config.db_path}-shm"),
    ):
        assert_private_file(path, identity, required=False)
    for path in (config.chat_work_json, config.venture_report_json):
        # These generated inputs remain protected by the 0700 state directory.
        # Normal runtime commands tighten them to 0600 before use.
        assert_owned_regular_file(path, identity, required=False)
    return config


def configure_runtime(
    args: argparse.Namespace,
    identity: RuntimeIdentity,
) -> dict[str, Any]:
    state_dir = resolve_cli_path(args.state_dir, default_state_dir(identity))
    config_path = resolve_cli_path(args.config, default_config_path(identity))
    projects_root = lexical_absolute_path(args.projects_root)
    additional_roots = tuple(
        lexical_absolute_path(path) for path in args.additional_projects_root
    )
    try:
        projects_roots = hub.normalize_projects_roots(
            projects_root,
            additional_roots,
        )
    except hub.RepoDiscoveryError as exc:
        raise RuntimeContractError(str(exc)) from exc
    refusal = safe_entry.projects_roots_refusal(
        projects_roots[0],
        projects_roots[1:],
    )
    if refusal:
        raise RuntimeContractError(refusal)

    repo_registry = lexical_absolute_path(
        args.repo_registry
        if args.repo_registry is not None
        else hub.resolve_repo_registry_path(projects_roots[0], None)
    )
    config = RuntimeConfig(
        identity=identity,
        state_dir=state_dir,
        db_path=state_dir / "control_hub.db",
        backups_dir=state_dir / "backups",
        chat_work_json=state_dir / "chat_work_brief.json",
        venture_report_json=state_dir / "venture_autonomy_report.json",
        projects_roots=projects_roots,
        repo_registry=repo_registry,
        host=args.host,
        port=args.port,
        window_tracking=args.enable_window_tracking,
    )
    # Round-trip through strict validation before writing.
    RuntimeConfig.from_dict(config.to_dict())

    ensure_private_directory(config_path.parent, identity)
    prepare_runtime_paths(config, identity)
    if config_path.exists() or config_path.is_symlink():
        assert_private_file(config_path, identity)
        existing = RuntimeConfig.from_dict(
            read_bounded_json(config_path, MAX_CONFIG_BYTES)
        )
        validate_identity(existing, identity)
        if not args.replace_config:
            raise RuntimeContractError(
                f"runtime config already exists: {config_path}; "
                "use --replace-config after reviewing the new boundary"
            )

    atomic_write(
        config_path,
        json.dumps(config.to_dict(), indent=2, sort_keys=True) + "\n",
        0o600,
    )
    assert_private_file(config_path, identity)
    return {
        "status": "configured",
        "identity": {
            "user": identity.user,
            "uid": identity.uid,
            "gid": identity.gid,
        },
        "config": str(config_path),
        "state_dir": str(config.state_dir),
        "db_path": str(config.db_path),
        "backups_dir": str(config.backups_dir),
        "projects_roots": [str(path) for path in config.projects_roots],
        "repo_registry": str(config.repo_registry),
        "host": config.host,
        "port": config.port,
        "window_tracking": config.window_tracking,
    }


def database_integrity(path: Path) -> None:
    try:
        uri = f"{path.as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            rows = conn.execute("PRAGMA quick_check").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise RuntimeContractError(f"unable to inspect SQLite database {path}: {exc}") from exc
    if rows != [("ok",)]:
        raise RuntimeContractError(f"SQLite integrity check failed for {path}: {rows}")


def check_runtime(
    config_path: Path,
    identity: RuntimeIdentity,
) -> dict[str, Any]:
    config = load_runtime_config(config_path, identity)
    database_exists = config.db_path.exists()
    if database_exists:
        database_integrity(config.db_path)
    roots = []
    for root in config.projects_roots:
        error = safe_entry.projects_root_error(root)
        roots.append(
            {
                "path": str(root),
                "observable": error is None,
                "error": error,
            }
        )
    return {
        "status": "ok",
        "identity": {
            "user": identity.user,
            "uid": identity.uid,
            "gid": identity.gid,
        },
        "config": str(config_path),
        "config_mode": f"{mode_bits(config_path):04o}",
        "state_dir": str(config.state_dir),
        "state_dir_mode": f"{mode_bits(config.state_dir):04o}",
        "database": str(config.db_path),
        "database_exists": database_exists,
        "database_integrity": "ok" if database_exists else "not-created",
        "backups_dir": str(config.backups_dir),
        "projects_roots": roots,
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RuntimeContractError(f"unable to hash {path}: {exc}") from exc
    return digest.hexdigest()


@contextlib.contextmanager
def runtime_lock(
    config: RuntimeConfig,
    identity: RuntimeIdentity,
) -> Iterator[None]:
    lock_path = config.state_dir / "runtime.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise RuntimeContractError(f"unable to open runtime lock {lock_path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != identity.uid:
            raise RuntimeContractError(f"runtime lock ownership/type mismatch: {lock_path}")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextlib.contextmanager
def serve_boundary_lock(
    config: RuntimeConfig,
    identity: RuntimeIdentity,
) -> Iterator[None]:
    lock_path = config.state_dir / "serve.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise RuntimeContractError(
            f"unable to open serve boundary lock {lock_path}: {exc}"
        ) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != identity.uid:
            raise RuntimeContractError(
                f"serve boundary lock ownership/type mismatch: {lock_path}"
            )
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeContractError(
                "Control Hub serve runtime is active; stop it before "
                "migration or restore"
            ) from exc
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def sqlite_copy(source: Path, destination: Path) -> None:
    try:
        source_conn = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
        destination_conn = sqlite3.connect(destination)
        try:
            source_conn.backup(destination_conn)
            destination_conn.commit()
        finally:
            destination_conn.close()
            source_conn.close()
    except sqlite3.Error as exc:
        raise RuntimeContractError(
            f"SQLite copy failed from {source} to {destination}: {exc}"
        ) from exc
    destination.chmod(0o600)
    database_integrity(destination)


def place_database_without_overwrite(
    temporary_path: Path,
    destination: Path,
) -> None:
    try:
        os.link(temporary_path, destination, follow_symlinks=False)
    except FileExistsError as exc:
        raise RuntimeContractError(
            f"authoritative database appeared during recovery: {destination}"
        ) from exc
    except OSError as exc:
        raise RuntimeContractError(
            f"unable to place authoritative database {destination}: {exc}"
        ) from exc
    try:
        temporary_path.unlink()
    except OSError as exc:
        with contextlib.suppress(FileNotFoundError):
            destination.unlink()
        raise RuntimeContractError(
            f"unable to remove recovery temporary file {temporary_path}: {exc}"
        ) from exc


def temporary_database_path(directory: Path, prefix: str) -> Path:
    try:
        fd, path_text = tempfile.mkstemp(prefix=prefix, suffix=".db", dir=directory)
        os.close(fd)
        path = Path(path_text)
        path.chmod(0o600)
        return path
    except OSError as exc:
        raise RuntimeContractError(
            f"unable to allocate temporary database in {directory}: {exc}"
        ) from exc


def create_backup(
    config_path: Path,
    identity: RuntimeIdentity,
) -> dict[str, Any]:
    config = load_runtime_config(config_path, identity)
    assert_private_file(config.db_path, identity)
    database_integrity(config.db_path)
    prepare_runtime_paths(config, identity)

    with runtime_lock(config, identity):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        token = secrets.token_hex(4)
        backup_name = f"control_hub-{stamp}-{token}.db"
        backup_path = config.backups_dir / backup_name
        manifest_path = config.backups_dir / f"{backup_name}.manifest.json"
        temp_path = temporary_database_path(config.backups_dir, ".backup-")
        try:
            sqlite_copy(config.db_path, temp_path)
            os.replace(temp_path, backup_path)
            temp_path = Path()
            backup_path.chmod(0o600)
            manifest = {
                "schema_version": BACKUP_MANIFEST_SCHEMA_VERSION,
                "created_at": datetime.now(timezone.utc).replace(
                    microsecond=0
                ).isoformat(),
                "identity": {
                    "uid": identity.uid,
                    "gid": identity.gid,
                    "user": identity.user,
                },
                "source_db": str(config.db_path),
                "backup_file": backup_name,
                "size_bytes": backup_path.stat().st_size,
                "sha256": sha256_file(backup_path),
                "integrity": "ok",
            }
            atomic_write(
                manifest_path,
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                0o600,
            )
        finally:
            if temp_path != Path():
                with contextlib.suppress(FileNotFoundError):
                    temp_path.unlink()

    return {
        "status": "created",
        "backup": str(backup_path),
        "manifest": str(manifest_path),
        "sha256": manifest["sha256"],
        "size_bytes": manifest["size_bytes"],
        "integrity": "ok",
    }


def load_and_verify_backup(
    config: RuntimeConfig,
    manifest_path: Path,
    identity: RuntimeIdentity,
) -> tuple[dict[str, Any], Path]:
    manifest_path = lexical_absolute_path(manifest_path)
    if manifest_path.parent != config.backups_dir:
        raise RuntimeContractError(
            f"backup manifest must be directly inside {config.backups_dir}"
        )
    assert_private_file(manifest_path, identity)
    manifest = read_bounded_json(manifest_path, MAX_MANIFEST_BYTES)
    if not isinstance(manifest, dict):
        raise RuntimeContractError("backup manifest must be a JSON object")
    required = {
        "schema_version",
        "created_at",
        "identity",
        "source_db",
        "backup_file",
        "size_bytes",
        "sha256",
        "integrity",
    }
    if set(manifest) != required:
        raise RuntimeContractError("backup manifest keys mismatch")
    if manifest["schema_version"] != BACKUP_MANIFEST_SCHEMA_VERSION:
        raise RuntimeContractError("unsupported backup manifest schema")
    expected_identity = {
        "uid": identity.uid,
        "gid": identity.gid,
        "user": identity.user,
    }
    if manifest["identity"] != expected_identity:
        raise RuntimeContractError("backup manifest identity mismatch")
    if manifest["source_db"] != str(config.db_path):
        raise RuntimeContractError("backup manifest source database mismatch")
    backup_name = manifest["backup_file"]
    if (
        not isinstance(backup_name, str)
        or not backup_name
        or Path(backup_name).name != backup_name
    ):
        raise RuntimeContractError("backup manifest contains an unsafe backup filename")
    backup_path = config.backups_dir / backup_name
    assert_private_file(backup_path, identity)
    if (
        isinstance(manifest["size_bytes"], bool)
        or not isinstance(manifest["size_bytes"], int)
        or manifest["size_bytes"] != backup_path.stat().st_size
    ):
        raise RuntimeContractError("backup size does not match manifest")
    if not isinstance(manifest["sha256"], str) or len(manifest["sha256"]) != 64:
        raise RuntimeContractError("backup manifest SHA-256 is invalid")
    actual_hash = sha256_file(backup_path)
    if not secrets.compare_digest(actual_hash, manifest["sha256"]):
        raise RuntimeContractError("backup SHA-256 does not match manifest")
    database_integrity(backup_path)
    return manifest, backup_path


def verify_backup(
    config_path: Path,
    manifest_path: Path,
    identity: RuntimeIdentity,
) -> dict[str, Any]:
    config = load_runtime_config(config_path, identity)
    manifest, backup_path = load_and_verify_backup(config, manifest_path, identity)
    return {
        "status": "verified",
        "backup": str(backup_path),
        "manifest": str(lexical_absolute_path(manifest_path)),
        "sha256": manifest["sha256"],
        "size_bytes": manifest["size_bytes"],
        "integrity": "ok",
    }


def authoritative_database_artifacts(
    config: RuntimeConfig,
) -> tuple[Path, Path, Path]:
    return (
        config.db_path,
        Path(f"{config.db_path}-wal"),
        Path(f"{config.db_path}-shm"),
    )


def assert_authoritative_database_absent(config: RuntimeConfig) -> None:
    present = [
        str(path)
        for path in authoritative_database_artifacts(config)
        if path.exists() or path.is_symlink()
    ]
    if present:
        raise RuntimeContractError(
            "authoritative database or SQLite sidecars already exist: "
            + ", ".join(present)
        )


def restore_backup(
    config_path: Path,
    manifest_path: Path,
    identity: RuntimeIdentity,
) -> dict[str, Any]:
    config = load_runtime_config(config_path, identity)
    assert_authoritative_database_absent(config)
    manifest, backup_path = load_and_verify_backup(config, manifest_path, identity)
    with serve_boundary_lock(config, identity):
        with runtime_lock(config, identity):
            assert_authoritative_database_absent(config)
            temp_path = temporary_database_path(config.state_dir, ".restore-")
            try:
                sqlite_copy(backup_path, temp_path)
                assert_authoritative_database_absent(config)
                place_database_without_overwrite(temp_path, config.db_path)
                temp_path = Path()
                config.db_path.chmod(0o600)
            finally:
                if temp_path != Path():
                    with contextlib.suppress(FileNotFoundError):
                        temp_path.unlink()
    return {
        "status": "restored",
        "database": str(config.db_path),
        "manifest": str(lexical_absolute_path(manifest_path)),
        "sha256": manifest["sha256"],
        "integrity": "ok",
    }


def migrate_database(
    config_path: Path,
    source_db: Path,
    identity: RuntimeIdentity,
) -> dict[str, Any]:
    config = load_runtime_config(config_path, identity)
    source_db = lexical_absolute_path(source_db)
    if source_db == config.db_path:
        raise RuntimeContractError("migration source is already the authoritative database")
    assert_private_file(source_db, identity)
    database_integrity(source_db)
    assert_authoritative_database_absent(config)
    with serve_boundary_lock(config, identity):
        with runtime_lock(config, identity):
            assert_authoritative_database_absent(config)
            temp_path = temporary_database_path(config.state_dir, ".migrate-")
            try:
                sqlite_copy(source_db, temp_path)
                assert_authoritative_database_absent(config)
                place_database_without_overwrite(temp_path, config.db_path)
                temp_path = Path()
                config.db_path.chmod(0o600)
            finally:
                if temp_path != Path():
                    with contextlib.suppress(FileNotFoundError):
                        temp_path.unlink()
    return {
        "status": "migrated",
        "source": str(source_db),
        "database": str(config.db_path),
        "integrity": "ok",
    }


def safe_entry_arguments(config: RuntimeConfig, command: str) -> list[str]:
    args = [
        command,
        "--db",
        str(config.db_path),
        "--projects-root",
        str(config.projects_roots[0]),
        "--repo-registry",
        str(config.repo_registry),
        "--chat-work-json",
        str(config.chat_work_json),
        "--venture-report-json",
        str(config.venture_report_json),
    ]
    for root in config.projects_roots[1:]:
        args.extend(["--additional-projects-root", str(root)])
    if command == "scan-serve":
        args.extend(["--host", config.host, "--port", str(config.port)])
        if not config.window_tracking:
            args.append("--no-window-tracking")
    return args


def run_control_hub(
    config_path: Path,
    identity: RuntimeIdentity,
    *,
    serve: bool,
) -> int:
    config = load_runtime_config(config_path, identity)
    prepare_runtime_paths(config, identity)
    command = "scan-serve" if serve else "scan"
    if serve:
        with serve_boundary_lock(config, identity):
            return safe_entry.main(safe_entry_arguments(config, command))

    result = safe_entry.main(safe_entry_arguments(config, command))
    if result == 0:
        prepare_runtime_paths(config, identity)
        assert_private_file(config.db_path, identity)
    return result


def systemd_quote(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
    )
    return f'"{escaped}"'


def render_user_units(config_path: Path) -> dict[str, str]:
    repo_dir = Path(__file__).resolve().parents[1]
    fleetctl = repo_dir / "fleetctl"
    quoted_fleetctl = systemd_quote(str(fleetctl))
    quoted_config = systemd_quote(str(config_path))
    quoted_repo = systemd_quote(str(repo_dir))

    service = f"""[Unit]
Description=Fleet Control Hub non-root dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={quoted_repo}
ExecStart={quoted_fleetctl} hub-runtime serve --config {quoted_config}
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
RestrictSUIDSGID=true
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""
    backup_service = f"""[Unit]
Description=Fleet Control Hub verified SQLite backup

[Service]
Type=oneshot
WorkingDirectory={quoted_repo}
ExecStart={quoted_fleetctl} hub-runtime backup --config {quoted_config}
UMask=0077
NoNewPrivileges=true
RestrictSUIDSGID=true
"""
    backup_timer = f"""[Unit]
Description=Run Fleet Control Hub verified backup daily

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=900
Unit={BACKUP_SERVICE_NAME}

[Install]
WantedBy=timers.target
"""
    return {
        SERVICE_NAME: service,
        BACKUP_SERVICE_NAME: backup_service,
        BACKUP_TIMER_NAME: backup_timer,
    }


def install_user_service(
    config_path: Path,
    unit_dir: Path,
    identity: RuntimeIdentity,
    *,
    no_start: bool,
) -> dict[str, Any]:
    load_runtime_config(config_path, identity)
    unit_dir = lexical_absolute_path(unit_dir)
    assert_unit_directory(unit_dir, identity)
    units = render_user_units(config_path)
    for name, content in units.items():
        atomic_write(unit_dir / name, content, 0o644)

    if not no_start:
        systemctl = shutil.which("systemctl")
        if not systemctl:
            raise RuntimeContractError("systemctl is required to install user services")
        commands = [
            [systemctl, "--user", "daemon-reload"],
            [
                systemctl,
                "--user",
                "enable",
                "--now",
                SERVICE_NAME,
                BACKUP_TIMER_NAME,
            ],
        ]
        for command in commands:
            proc = subprocess.run(
                command,
                text=True,
                capture_output=True,
                check=False,
            )
            if proc.returncode != 0:
                detail = proc.stderr.strip() or proc.stdout.strip()
                raise RuntimeContractError(
                    f"systemctl command failed ({' '.join(command)}): {detail}"
                )

    return {
        "status": "installed",
        "identity": {
            "user": identity.user,
            "uid": identity.uid,
            "gid": identity.gid,
        },
        "unit_dir": str(unit_dir),
        "units": [str(unit_dir / name) for name in UNIT_NAMES],
        "started": not no_start,
        "state_preserved_on_uninstall": True,
    }


def print_json(value: dict[str, Any]) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def add_config_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Runtime config path (default: XDG config home/fleet-control-hub/runtime.json).",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the explicit non-root Fleet Control Hub runtime contract."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    configure = subparsers.add_parser(
        "configure",
        help="Bind one non-root identity, state directory, and reviewed root set.",
    )
    add_config_option(configure)
    configure.add_argument("--state-dir", type=Path, default=None)
    configure.add_argument("--projects-root", type=Path, required=True)
    configure.add_argument(
        "--additional-projects-root",
        type=Path,
        action="append",
        default=[],
    )
    configure.add_argument("--repo-registry", type=Path, default=None)
    configure.add_argument("--host", choices=("127.0.0.1", "::1"), default="127.0.0.1")
    configure.add_argument("--port", type=int, default=8765)
    configure.add_argument("--enable-window-tracking", action="store_true")
    configure.add_argument("--replace-config", action="store_true")

    check = subparsers.add_parser("check", help="Validate identity, ownership, modes, and DB.")
    add_config_option(check)

    scan = subparsers.add_parser("scan", help="Run a guarded scan from the bound config.")
    add_config_option(scan)

    serve = subparsers.add_parser(
        "serve",
        help="Run guarded scan-serve from the bound config.",
    )
    add_config_option(serve)

    backup = subparsers.add_parser(
        "backup",
        help="Create an online SQLite backup plus SHA-256 manifest.",
    )
    add_config_option(backup)

    verify = subparsers.add_parser(
        "verify-backup",
        help="Verify backup identity, mode, SHA-256, size, and SQLite integrity.",
    )
    add_config_option(verify)
    verify.add_argument("--manifest", type=Path, required=True)

    restore = subparsers.add_parser(
        "restore",
        help="Restore a verified backup only when the authoritative DB is absent.",
    )
    add_config_option(restore)
    restore.add_argument("--manifest", type=Path, required=True)

    migrate = subparsers.add_parser(
        "migrate",
        help="Copy a same-owner private SQLite DB into an absent authoritative path.",
    )
    add_config_option(migrate)
    migrate.add_argument("--from-db", type=Path, required=True)

    install = subparsers.add_parser(
        "install-user-service",
        help="Install the identity-bound user service and daily backup timer.",
    )
    add_config_option(install)
    install.add_argument(
        "--unit-dir",
        type=Path,
        default=None,
        help="Override the systemd user unit directory.",
    )
    install.add_argument(
        "--no-start",
        action="store_true",
        help="Render units without invoking systemctl.",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    old_umask = os.umask(0o077)
    try:
        identity = current_identity()
        require_non_root(identity)
        config_path = resolve_cli_path(
            getattr(args, "config", None),
            default_config_path(identity),
        )

        if args.command == "configure":
            print_json(configure_runtime(args, identity))
            return 0
        if args.command == "check":
            print_json(check_runtime(config_path, identity))
            return 0
        if args.command == "scan":
            return run_control_hub(config_path, identity, serve=False)
        if args.command == "serve":
            return run_control_hub(config_path, identity, serve=True)
        if args.command == "backup":
            print_json(create_backup(config_path, identity))
            return 0
        if args.command == "verify-backup":
            print_json(verify_backup(config_path, args.manifest, identity))
            return 0
        if args.command == "restore":
            print_json(restore_backup(config_path, args.manifest, identity))
            return 0
        if args.command == "migrate":
            print_json(migrate_database(config_path, args.from_db, identity))
            return 0
        if args.command == "install-user-service":
            unit_dir = resolve_cli_path(
                args.unit_dir,
                default_unit_dir(identity),
            )
            print_json(
                install_user_service(
                    config_path,
                    unit_dir,
                    identity,
                    no_start=args.no_start,
                )
            )
            return 0
        parser.error(f"unsupported command: {args.command}")
    except RuntimeContractError as exc:
        print(f"[control-hub-runtime] refused: {exc}", file=sys.stderr)
        return 2
    finally:
        os.umask(old_umask)


if __name__ == "__main__":
    raise SystemExit(main())
