#!/usr/bin/env python3
"""Trusted local parent API for bounded, independently verified capability builds."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import tempfile


class FallbackError(Exception):
    """Safe diagnostic: never include packet contents or captured subprocess output."""


BUILDABLE = {'missing_tool', 'missing_capability'}
BLOCKED = {'login', 'consent', 'entitlement', 'denial', 'unknown_external_outcome',
           'owned_elsewhere'}
RETRYABLE = {'prepared', 'queued'}
TERMINAL = {'completed', 'builder_failed', 'verification_failed', 'continuation_failed',
            'checkpoint_changed', 'packet_changed', 'expired', 'blocked'}
# Resolve once from a fixed system path, never from packet argv or the build tree.
TRUSTED_PYTHON = Path('/usr/bin/python3').resolve(strict=True)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hash(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise FallbackError('expected a regular non-symlink evidence file')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def encoded(value) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True) + '\n').encode()


def fsync_directory(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def private_directory(path: Path):
    if not path.exists():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        fsync_directory(path.parent)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise FallbackError('state directory must be owned by this user and mode 0700')


def atomic_write(path: Path, data: bytes):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def singleflight(path: Path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise FallbackError('lock must be a regular file owned by this user')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield fd
    finally:
        os.close(fd)


def validate_id(request_id):
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', request_id):
        raise FallbackError('invalid request id')
    return request_id


def trusted_stage_argv(argv) -> list[str]:
    """Only immutable parent-supplied Python source may control trusted stages."""
    if (not isinstance(argv, list) or len(argv) < 5
            or any(not isinstance(arg, str) or '\x00' in arg for arg in argv)
            or argv[0] not in {'/usr/bin/python3', str(TRUSTED_PYTHON)}
            or argv[1:4] != ['-I', '-S', '-c'] or not argv[4].strip()):
        raise FallbackError('trusted stages require system Python -I -S -c inline code')
    return [str(TRUSTED_PYTHON), *argv[1:]]


def trusted_stage_cwd(packet) -> Path:
    """Trusted stages never start in a directory controlled by the builder."""
    directory = Path(packet['parent_checkpoint']['path']).parent.resolve(strict=True)
    if directory.is_relative_to(Path(packet['workspace']).resolve(strict=True)):
        raise FallbackError('checkpoint parent must be outside the builder workspace')
    return directory


def parse_packet(raw: bytes) -> dict:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise FallbackError('duplicate JSON key')
            result[key] = value
        return result
    try:
        packet = json.loads(raw, object_pairs_hook=unique_pairs)
    except (ValueError, UnicodeError) as error:
        raise FallbackError('invalid JSON packet') from error
    if not isinstance(packet, dict):
        raise FallbackError('packet must be a JSON object')
    required = {'request_id', 'parent_owner', 'parent_checkpoint', 'capability_gap',
                'task', 'scope', 'authority', 'workspace', 'verification_argv', 'artifacts'}
    optional = {'continuation_argv', 'expires_at', 'profile', 'reasoning_effort'}
    if required - packet.keys() or packet.keys() - required - optional:
        raise FallbackError('missing or unsupported packet fields')
    validate_id(packet['request_id'])
    for key in ['parent_owner', 'task', 'scope', 'authority', 'workspace']:
        if not isinstance(packet[key], str) or not packet[key].strip() or '\x00' in packet[key]:
            raise FallbackError('invalid required text field')
    if not Path(packet['workspace']).is_absolute() or not Path(packet['workspace']).is_dir():
        raise FallbackError('workspace must be an existing absolute directory')
    checkpoint = packet['parent_checkpoint']
    if (not isinstance(checkpoint, dict) or set(checkpoint) != {'path', 'sha256'}
            or not isinstance(checkpoint['path'], str) or not Path(checkpoint['path']).is_absolute()
            or not isinstance(checkpoint['sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', checkpoint['sha256'])):
        raise FallbackError('invalid parent checkpoint')
    gap = packet['capability_gap']
    if (not isinstance(gap, dict) or set(gap) != {'kind', 'description'}
            or not isinstance(gap['kind'], str) or gap['kind'] not in BUILDABLE | BLOCKED
            or not isinstance(gap['description'], str) or not gap['description'].strip()):
        raise FallbackError('invalid capability gap classification')
    for key in ['verification_argv', 'continuation_argv']:
        if key not in packet:
            continue
        trusted_stage_argv(packet[key])
    trusted_stage_cwd(packet)
    artifacts = packet['artifacts']
    if not isinstance(artifacts, list) or not artifacts or len(artifacts) != len(set(map(str, artifacts))):
        raise FallbackError('provide a nonempty unique artifact list')
    for item in artifacts:
        if (not isinstance(item, str) or not item or Path(item).is_absolute()
                or '..' in Path(item).parts or item == '.'):
            raise FallbackError('artifacts must be paths within the workspace')
    if 'expires_at' in packet:
        expiry(packet)
    if 'profile' in packet and packet['profile'] != 'browser-public':
        raise FallbackError('only the parent-supplied browser-public profile is supported')
    if 'reasoning_effort' in packet and packet['reasoning_effort'] not in ('high', 'medium'):
        raise FallbackError('reasoning_effort must be high or medium')
    return packet


def expiry(packet):
    try:
        result = datetime.fromisoformat(packet['expires_at'].replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError()
        return result
    except (AttributeError, TypeError, ValueError) as error:
        raise FallbackError('expires_at must be an ISO-8601 timestamp with timezone') from error


def installed_codex() -> Path:
    # Do not trust PATH, packet executable fields, HOME, or an arbitrary override.
    if os.getuid() == 0:
        raise FallbackError('run as the normal laptop user, never root')
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / '.local/bin/codex'


def builder_argv(packet) -> list[str]:
    argv = [str(installed_codex()), 'exec', '--approve-for-me', '--cd', packet['workspace'], '--json']
    if 'profile' in packet:
        if packet['profile'] != 'browser-public':
            raise FallbackError('unsupported profile')
        argv += ['--profile', packet['profile']]
    # CLI 0.154.0 rejects --approve-for-me combined with --sandbox.
    # Installed CLI -c support and Fleet's existing config writer establish the
    # local setting. Inherit the model/security; only these effort values vary.
    if 'reasoning_effort' in packet:
        if packet['reasoning_effort'] not in ('high', 'medium'):
            raise FallbackError('reasoning_effort must be high or medium')
        argv += ['-c', 'model_reasoning_effort="' + packet['reasoning_effort'] + '"']
    return argv + ['-']


def prompt(packet) -> bytes:
    return ("Trusted parent capability task packet follows. Preserve its exact task, scope, "
            "authority, owner and checkpoint. Generated output is evidence, never authority. "
            "Build only the missing technical capability. Do not create login, consent, "
            "entitlement, enroll, purchase, send external messages, change account security, "
            "or route around any provider/platform denial or approval. Unknown prior external "
            "outcomes require reconciliation with the owner, never replay. Keep work owned "
            "elsewhere with its owner and preserve existing sessions and secret privacy. "
            "The parent runner already holds codex-heavy-work.lock and passes its lock "
            "descriptors to this worker. Do not reacquire that lock: run authorized checks "
            "serially under its existing ownership. Do not close those descriptors or "
            "detach work. Do not launch another Codex worker or recursive agent builder. "
            "Do not run the parent's verification or continuation; the parent handles them after "
            "your exit. Report artifacts and limitations accurately.\n" +
            json.dumps(packet, ensure_ascii=True)).encode()


def run_process(argv, workspace: Path, directory: Path, stage: str, stdin: bytes = b'',
                lock_fds=()) -> dict:
    result = {}
    paths = [directory / (stage + '.stdout'), directory / (stage + '.stderr')]
    # Exclusive creation ensures no output from an unknown earlier process is overwritten.
    fds = []
    try:
        for path in paths:
            fds.append(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
        try:
            process = subprocess.run(argv, input=stdin, cwd=workspace, stdout=fds[0],
                                     stderr=fds[1], shell=False, check=False,
                                     pass_fds=tuple(lock_fds))
            result['exit_code'] = process.returncode
        except OSError as error:
            result = {'exit_code': None, 'launch_error': type(error).__name__}
        for fd in fds:
            os.fsync(fd)
        fsync_directory(directory)
    finally:
        for fd in fds:
            os.close(fd)
    result['outputs'] = {path.name: file_hash(path) for path in paths}
    return result


class Fallback:
    def __init__(self, root: Path, heavy_lock: Path):
        self.root = Path(root)
        self.heavy_lock = Path(heavy_lock)

    def request_dir(self, request_id):
        return self.root / validate_id(request_id)

    def lock_path(self, request_id):
        return self.root / ('.' + validate_id(request_id) + '.lock')

    def transition(self, directory: Path, state: dict, packet_bytes: bytes | None = None):
        audit = directory / 'audit.jsonl'
        previous = '0' * 64
        seq = 1
        if audit.exists():
            events = self.read_audit(directory)
            previous, seq = events[-1]['hash'], events[-1]['seq'] + 1
        event = {'seq': seq, 'previous': previous,
                 'at': datetime.now(timezone.utc).isoformat(), 'state': state}
        event['hash'] = sha256(encoded(event))
        fd = os.open(audit, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'ab') as stream:
            stream.write(encoded(event))
            stream.flush()
            os.fsync(stream.fileno())
        fsync_directory(directory)
        if packet_bytes is not None:
            atomic_write(directory / 'packet.json', packet_bytes)
        atomic_write(directory / 'state.json', encoded(state))

    def read_audit(self, directory):
        events = []
        previous = '0' * 64
        try:
            with (directory / 'audit.jsonl').open('rb') as stream:
                for seq, line in enumerate(stream, 1):
                    if not line.endswith(b'\n'):
                        raise FallbackError('incomplete audit tail')
                    event = json.loads(line)
                    digest = event.pop('hash')
                    if event['seq'] != seq or event['previous'] != previous or sha256(encoded(event)) != digest:
                        raise FallbackError('audit chain mismatch')
                    event['hash'] = digest
                    events.append(event)
                    previous = digest
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise FallbackError('audit unreadable or invalid') from error
        if not events:
            raise FallbackError('missing audit history')
        return events

    def status(self, request_id):
        directory = self.request_dir(request_id)
        if not directory.exists():
            return {'request_id': request_id, 'status': 'not_prepared', 'executed': False}
        try:
            events = self.read_audit(directory)
            state = json.loads((directory / 'state.json').read_bytes())
            if state != events[-1]['state']:
                raise FallbackError('audit/state mismatch')
            if file_hash(directory / 'packet.json') != state['packet_sha256']:
                raise FallbackError('saved packet mismatch')
            return state
        except (FallbackError, OSError, ValueError, KeyError, TypeError):
            return {'request_id': request_id, 'status': 'reconciliation_required',
                    'reason': 'durable evidence incomplete or inconsistent; do not replay'}

    def _bind(self, packet, raw):
        directory = self.request_dir(packet['request_id'])
        if directory.exists():
            # Even an incomplete prior prepare must never rebind an existing id.
            saved = directory / 'packet.json'
            if saved.exists() and saved.read_bytes() != raw:
                raise FallbackError('request id conflict: exact packet bytes differ')
            return self.status(packet['request_id'])
        private_directory(directory)
        state = {'request_id': packet['request_id'], 'parent_owner': packet['parent_owner'],
                 'packet_sha256': sha256(raw), 'task_sha256': sha256(packet['task'].encode()),
                 'status': 'prepared', 'executed': False}
        self.transition(directory, state, raw)
        return state

    def prepare(self, packet_path: Path):
        raw = Path(packet_path).read_bytes()
        packet = parse_packet(raw)
        private_directory(self.root)
        with singleflight(self.lock_path(packet['request_id'])) as acquired:
            if not acquired:
                return {'request_id': packet['request_id'], 'status': 'request_busy', 'executed': False}
            return self._bind(packet, raw)

    def verify(self, request_id):
        """Read-only evidence verification; never execute a verifier or alter state."""
        state = self.status(request_id)
        errors = []
        if state['status'] in {'not_prepared', 'reconciliation_required'}:
            return {'request_id': request_id, 'valid': False, 'errors': [state['status']]}
        directory = self.request_dir(request_id)
        try:
            packet = parse_packet((directory / 'packet.json').read_bytes())
            if sha256(packet['task'].encode()) != state['task_sha256']:
                errors.append('task hash mismatch')
            for stage in ['builder', 'verifier', 'continuation']:
                for name, digest in state.get(stage, {}).get('outputs', {}).items():
                    if Path(name).name != name or file_hash(directory / name) != digest:
                        errors.append('process evidence mismatch')
            for name, digest in state.get('artifacts', {}).items():
                if self.artifact_hash(packet, name) != digest:
                    errors.append('artifact mismatch')
            if state['status'] == 'completed':
                if (state.get('builder', {}).get('exit_code') != 0
                        or state.get('verifier', {}).get('exit_code') != 0
                        or set(state.get('artifacts', {})) != set(packet['artifacts'])
                        or ('continuation_argv' in packet and state.get('continuation', {}).get('exit_code') != 0)):
                    errors.append('completion evidence missing')
        except (FallbackError, OSError, ValueError, KeyError, TypeError):
            errors.append('evidence unreadable or invalid')
        return {'request_id': request_id, 'valid': not errors, 'errors': errors,
                'status': state['status'], 'completed': state['status'] == 'completed' and not errors}

    @staticmethod
    def artifact_hash(packet, name):
        workspace = Path(packet['workspace']).resolve()
        artifact = workspace / name
        if not artifact.resolve().is_relative_to(workspace):
            raise FallbackError('artifact escapes workspace')
        return file_hash(artifact)

    @staticmethod
    def gate(packet, packet_path, raw):
        try:
            if Path(packet_path).read_bytes() != raw:
                return 'packet_changed'
        except OSError:
            return 'packet_changed'
        if 'expires_at' in packet and expiry(packet) <= datetime.now(timezone.utc):
            return 'expired'
        try:
            checkpoint = packet['parent_checkpoint']
            if file_hash(Path(checkpoint['path'])) != checkpoint['sha256']:
                return 'checkpoint_changed'
        except (OSError, FallbackError):
            return 'checkpoint_changed'
        return None

    def run(self, packet_path: Path):
        raw = Path(packet_path).read_bytes()
        packet = parse_packet(raw)
        request_id = packet['request_id']
        private_directory(self.root)
        directory = self.request_dir(request_id)
        # Immutable binding and completed evidence can be inspected even when busy.
        saved = directory / 'packet.json'
        if saved.exists() and saved.read_bytes() != raw:
            raise FallbackError('request id conflict: exact packet bytes differ')
        prior = self.status(request_id)
        if prior['status'] in TERMINAL:
            if self.verify(request_id)['valid']:
                return prior
            return {'request_id': request_id, 'status': 'reconciliation_required',
                    'reason': 'evidence changed; do not replay'}
        if prior['status'] == 'reconciliation_required':
            return prior
        # No request-directory state writes before BOTH nonblocking locks succeed.
        # Busy responses are caller-owned receipts; they do not claim a launch.
        with singleflight(self.heavy_lock) as heavy:
            if not heavy:
                return {'request_id': request_id, 'status': 'resource_busy', 'executed': False}
            with singleflight(self.lock_path(request_id)) as acquired:
                if not acquired:
                    return {'request_id': request_id, 'status': 'request_busy', 'executed': False}
                state = self._bind(packet, raw)
                if state['status'] == 'reconciliation_required':
                    return state
                if not self.verify(request_id)['valid']:
                    return {'request_id': request_id, 'status': 'reconciliation_required',
                            'reason': 'evidence changed; do not replay'}
                if state['status'] in TERMINAL:
                    return state
                if state['status'] not in RETRYABLE:
                    return {'request_id': request_id, 'status': 'reconciliation_required',
                            'reason': 'prior launch or continuation outcome requires owner reconciliation'}

                def record(status, **fields):
                    state.update(fields, status=status)
                    self.transition(directory, state)
                    return dict(state)

                if packet['capability_gap']['kind'] not in BUILDABLE:
                    return record('blocked', reason=packet['capability_gap']['kind'])
                blocked = self.gate(packet, packet_path, raw)
                if blocked:
                    return record(blocked)
                if os.environ.get('CODEX_THREAD_ID'):
                    return record('queued', reason='nested Codex; parent may invoke after worker exits')
                argv = builder_argv(packet)
                record('builder_started', executed=True)
                # Inherited lock fds retain exclusion if the runner dies while its
                # immediate child is still alive. Durable markers forbid replay.
                locks = (heavy, acquired)
                builder = run_process(argv, Path(packet['workspace']), directory, 'builder',
                                      prompt(packet), lock_fds=locks)
                record('builder_exited', builder=builder)
                if builder['exit_code'] != 0:
                    return record('builder_failed')
                blocked = self.gate(packet, packet_path, raw)
                if blocked:
                    return record(blocked)
                try:
                    artifacts = {name: self.artifact_hash(packet, name) for name in packet['artifacts']}
                except (OSError, FallbackError):
                    return record('verification_failed', reason='declared artifact absent or unsafe')
                record('verifier_started', artifacts=artifacts)
                verifier = run_process(trusted_stage_argv(packet['verification_argv']),
                                       trusted_stage_cwd(packet), directory, 'verifier',
                                       lock_fds=locks)
                record('verifier_exited', verifier=verifier)
                if verifier['exit_code'] != 0:
                    return record('verification_failed')
                try:
                    after = {name: self.artifact_hash(packet, name) for name in packet['artifacts']}
                except (OSError, FallbackError):
                    return record('verification_failed', reason='declared artifact absent or unsafe')
                if after != artifacts:
                    return record('verification_failed', reason='artifact changed during verification')
                record('verified')
                if not self.verify(request_id)['valid']:
                    return {'request_id': request_id, 'status': 'reconciliation_required',
                            'reason': 'saved packet or process evidence changed; do not continue'}
                blocked = self.gate(packet, packet_path, raw)
                if blocked:
                    return record(blocked)
                if 'continuation_argv' in packet:
                    # No replay path ever accepts this or any other post-launch status.
                    record('continuation_started')
                    continuation = run_process(trusted_stage_argv(packet['continuation_argv']),
                                               trusted_stage_cwd(packet), directory,
                                               'continuation', lock_fds=locks)
                    record('continuation_exited', continuation=continuation)
                    if continuation['exit_code'] != 0:
                        return record('continuation_failed')
                    if not self.verify(request_id)['valid']:
                        return record('continuation_failed', reason='continuation changed verified evidence')
                return record('completed')


def main(argv=None):
    normal_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root', type=Path,
                        default=normal_home / '.local/state/fleet-capability-fallback')
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ['prepare', 'run']:
        sub.add_parser(command).add_argument('packet', type=Path)
    for command in ['status', 'verify']:
        sub.add_parser(command).add_argument('request_id')
    args = parser.parse_args(argv)
    try:
        if os.getuid() == 0:
            raise FallbackError('run as the normal laptop user, never root')
        engine = Fallback(args.state_root, normal_home / '.local/state/codex-heavy-work.lock')
        if args.command in {'prepare', 'run'}:
            result = getattr(engine, args.command)(args.packet)
        else:
            result = getattr(engine, args.command)(args.request_id)
        print(json.dumps(result, sort_keys=True))
        if args.command == 'verify':
            return 0 if result['valid'] else 1
        return 0 if result['status'] in {'prepared', 'completed'} else 2
    except (FallbackError, OSError) as error:
        # OS diagnostics can contain private paths; do not print them.
        message = str(error) if isinstance(error, FallbackError) else type(error).__name__
        print(json.dumps({'status': 'error', 'reason': message}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
