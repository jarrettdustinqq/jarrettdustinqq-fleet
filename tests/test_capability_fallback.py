"""Offline contract tests: real small processes, never a live Codex worker."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    'fallback', Path(__file__).resolve().parents[1] / 'ops/capability_fallback.py')
cf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cf)


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.workspace = self.base / 'work'
        self.workspace.mkdir()
        self.checkpoint = self.base / 'checkpoint'
        self.checkpoint.write_text('owned checkpoint')
        self.packet = self.base / 'packet.json'
        self.data = {
            'request_id': 'offline-1', 'parent_owner': 'test-parent',
            'parent_checkpoint': {'path': str(self.checkpoint),
                                  'sha256': cf.sha256(self.checkpoint.read_bytes())},
            'capability_gap': {'kind': 'missing_tool', 'description': 'local helper'},
            'task': 'Build a local artifact.', 'scope': 'Only this test workspace.',
            'authority': 'Local test files and local subprocesses only.',
            'workspace': str(self.workspace), 'artifacts': ['artifact.txt'],
            'verification_argv': self.stage(
                "import sys; from pathlib import Path; assert Path(sys.argv[1]).read_text() == 'built'; print('verified independently')",
                self.workspace / 'artifact.txt'),
            'continuation_argv': self.stage(
                "import sys; from pathlib import Path; Path(sys.argv[1]).write_text('once')",
                self.workspace / 'continued'),
        }
        self.save()
        self.engine = cf.Fallback(self.base / 'state', self.base / 'heavy.lock')
        self.builder_code = (
            "import sys; from pathlib import Path; "
            "assert 'test-parent' in sys.stdin.read(); "
            "p=Path('build-count'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); "
            "Path('artifact.txt').write_text('built'); print('{\"type\":\"turn.completed\"}')")
        self.patch = mock.patch.object(cf, 'builder_argv',
            side_effect=lambda packet: [sys.executable, '-c', self.builder_code])
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop('CODEX_THREAD_ID', None)
        self.addCleanup(self.env.stop)

    @staticmethod
    def stage(code, *args):
        return [str(cf.TRUSTED_PYTHON), '-I', '-S', '-c', code, *map(str, args)]

    def save(self):
        self.packet.write_text(json.dumps(self.data))

    def run_task(self):
        return self.engine.run(self.packet)

    def test_success_independent_verifier_duplicate_and_readonly_verify(self):
        state = self.run_task()
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['builder']['exit_code'], 0)
        self.assertEqual(state['verifier']['exit_code'], 0)
        self.assertEqual(state['continuation']['exit_code'], 0)
        self.assertEqual(state['artifacts']['artifact.txt'], cf.sha256(b'built'))
        self.assertEqual(state['task_sha256'], cf.sha256(self.data['task'].encode()))
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in self.engine.request_dir('offline-1').iterdir() if p.is_file()}
        self.assertTrue(self.engine.verify('offline-1')['valid'])
        self.assertEqual(self.run_task(), state)
        self.assertEqual((self.workspace / 'build-count').read_text(), '1')
        after = {p: (p.read_bytes(), p.stat().st_mtime_ns)
                 for p in self.engine.request_dir('offline-1').iterdir() if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(self.engine.request_dir('offline-1').stat().st_mode & 0o777, 0o700)
        for p in before:
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_conflicting_exact_packet_bytes_rejected(self):
        self.engine.prepare(self.packet)
        self.packet.write_text(self.packet.read_text() + '\n')
        with self.assertRaisesRegex(cf.FallbackError, 'conflict'):
            self.run_task()
        self.assertFalse((self.workspace / 'build-count').exists())

    def test_resource_contention_records_no_execution_then_resumes(self):
        with (self.base / 'heavy.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            busy = self.run_task()
            self.assertEqual(busy['status'], 'resource_busy')
            self.assertFalse(busy['executed'])
            self.assertFalse(self.engine.request_dir('offline-1').exists())
        self.assertEqual(self.run_task()['status'], 'completed')

    def test_request_singleflight_real_competing_process(self):
        self.engine.prepare(self.packet)
        lock_path = self.engine.lock_path('offline-1')
        code = ("import fcntl,sys; f=open(sys.argv[1], 'a'); "
                "fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); sys.stdin.read()")
        with subprocess.Popen([sys.executable, '-c', code, str(lock_path)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as child:
            try:
                self.assertEqual(child.stdout.readline().strip(), 'ready')
                self.assertEqual(self.run_task()['status'], 'request_busy')
            finally:
                child.communicate('stop', timeout=5)
        self.assertFalse((self.workspace / 'build-count').exists())
        self.assertEqual(self.run_task()['status'], 'completed')

    def test_failed_verifier_prevents_continuation(self):
        self.data['verification_argv'] = self.stage('raise SystemExit(7)')
        self.save()
        state = self.run_task()
        self.assertEqual(state['status'], 'verification_failed')
        self.assertEqual(state['verifier']['exit_code'], 7)
        self.assertFalse((self.workspace / 'continued').exists())
        self.assertEqual(self.run_task(), state)

    def test_model_success_text_does_not_override_builder_exit(self):
        self.builder_code += "; print('SUCCESS'); raise SystemExit(2)"
        state = self.run_task()
        self.assertEqual(state['status'], 'builder_failed')
        self.assertEqual(state['builder']['exit_code'], 2)
        self.assertNotIn('verifier', state)
        self.assertFalse((self.workspace / 'continued').exists())

    def test_verifier_cannot_replace_artifact_and_claim_success(self):
        self.data['verification_argv'] = self.stage(
            "import sys; from pathlib import Path; Path(sys.argv[1]).write_text('replacement')",
            self.workspace / 'artifact.txt')
        self.save()
        self.assertEqual(self.run_task()['status'], 'verification_failed')
        self.assertFalse((self.workspace / 'continued').exists())

    def test_completed_and_conflicting_packets_resolve_while_resource_busy(self):
        state = self.run_task()
        with (self.base / 'heavy.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.run_task(), state)
            self.packet.write_text(self.packet.read_text() + '\n')
            with self.assertRaisesRegex(cf.FallbackError, 'conflict'):
                self.run_task()

    def test_no_continuation_completes_after_verifier(self):
        del self.data['continuation_argv']
        self.save()
        self.assertEqual(self.run_task()['status'], 'completed')
        self.assertFalse((self.workspace / 'continued').exists())

    def test_child_inherits_resource_and_request_locks(self):
        original = cf.run_process
        def check(argv, *args, **kwargs):
            for fd in kwargs['lock_fds']:
                os.fstat(fd)
            probe = "import os; " + '; '.join(
                'os.fstat(' + str(fd) + ')' for fd in kwargs['lock_fds'])
            result = subprocess.run([sys.executable, '-c', probe],
                                    pass_fds=kwargs['lock_fds'], check=False)
            self.assertEqual(result.returncode, 0)
            return original(argv, *args, **kwargs)
        with mock.patch.object(cf, 'run_process', side_effect=check):
            self.assertEqual(self.run_task()['status'], 'completed')

    def test_stale_checkpoint_prevents_continuation(self):
        self.data['verification_argv'] = self.stage(
            'from pathlib import Path; Path(' + repr(str(self.checkpoint)) + ").write_text('changed')")
        self.save()
        self.assertEqual(self.run_task()['status'], 'checkpoint_changed')
        self.assertFalse((self.workspace / 'continued').exists())

    def test_packet_changed_during_builder_prevents_continuation(self):
        self.builder_code += '; Path(' + repr(str(self.packet)) + ").write_text('{}')"
        self.assertEqual(self.run_task()['status'], 'packet_changed')
        self.assertFalse((self.workspace / 'continued').exists())

    def test_saved_packet_tampering_prevents_continuation(self):
        saved = self.engine.request_dir('offline-1') / 'packet.json'
        self.data['verification_argv'] = self.stage(
            'from pathlib import Path; Path(' + repr(str(saved)) + ").write_text('{}')")
        self.save()
        self.assertEqual(self.run_task()['status'], 'reconciliation_required')
        self.assertFalse((self.workspace / 'continued').exists())

    def test_interrupted_continuation_never_replays(self):
        original = cf.run_process
        def interrupt(argv, *args, **kwargs):
            if argv == self.data['continuation_argv']:
                (self.workspace / 'continued').write_text('unknown outcome')
                raise KeyboardInterrupt()
            return original(argv, *args, **kwargs)
        with mock.patch.object(cf, 'run_process', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_task()
        self.assertEqual(self.engine.status('offline-1')['status'], 'continuation_started')
        self.assertEqual(self.run_task()['status'], 'reconciliation_required')
        self.assertEqual((self.workspace / 'build-count').read_text(), '1')
        self.assertEqual((self.workspace / 'continued').read_text(), 'unknown outcome')

    def test_interrupted_builder_never_replays(self):
        with mock.patch.object(cf, 'run_process', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_task()
        self.assertEqual(self.run_task()['status'], 'reconciliation_required')
        self.assertFalse((self.workspace / 'build-count').exists())

    def test_nested_codex_queues_without_launch(self):
        with mock.patch.dict(os.environ, {'CODEX_THREAD_ID': 'parent-thread'}):
            self.assertEqual(self.run_task()['status'], 'queued')
        self.assertFalse((self.workspace / 'build-count').exists())
        self.assertEqual(self.run_task()['status'], 'completed')

    def test_denial_access_unknown_outcome_are_blocked(self):
        for kind in ['denial', 'login', 'consent', 'entitlement', 'unknown_external_outcome']:
            with self.subTest(kind=kind):
                self.data['request_id'] = kind
                self.data['capability_gap']['kind'] = kind
                self.save()
                self.assertEqual(self.run_task()['status'], 'blocked')
        self.assertFalse((self.workspace / 'build-count').exists())

    def test_expired_request_never_launches(self):
        self.data['expires_at'] = '2000-01-01T00:00:00Z'
        self.save()
        self.assertEqual(self.run_task()['status'], 'expired')
        self.assertFalse((self.workspace / 'build-count').exists())

    def test_artifact_tampering_is_detected_without_mutating(self):
        self.run_task()
        (self.workspace / 'artifact.txt').write_text('tampered')
        self.assertFalse(self.engine.verify('offline-1')['valid'])
        self.assertEqual(self.run_task()['status'], 'reconciliation_required')
        self.assertEqual((self.workspace / 'build-count').read_text(), '1')

    def test_audit_fsync_precedes_every_atomic_state_update(self):
        atomic = cf.atomic_write
        observed = []
        def inspect(path, data):
            if path.name == 'state.json':
                events = [json.loads(line) for line in (path.parent / 'audit.jsonl').read_text().splitlines()]
                self.assertEqual(events[-1]['state'], json.loads(data))
                observed.append(True)
            return atomic(path, data)
        with mock.patch.object(cf, 'atomic_write', side_effect=inspect):
            self.run_task()
        self.assertGreaterEqual(len(observed), 7)
        order = []
        real_fsync, real_replace = os.fsync, os.replace
        with mock.patch.object(os, 'fsync', side_effect=lambda fd: (order.append('fsync'), real_fsync(fd))[1]), \
             mock.patch.object(os, 'replace', side_effect=lambda a, b: (order.append('replace'), real_replace(a, b))[1]):
            cf.atomic_write(self.base / 'atomic', b'proof')
        self.assertEqual(order, ['fsync', 'replace', 'fsync'])
        order.clear()
        with mock.patch.object(os, 'fsync', side_effect=lambda fd: (order.append('fsync'), real_fsync(fd))[1]), \
             mock.patch.object(cf, 'atomic_write', side_effect=lambda *a: order.append('atomic')):
            self.engine.transition(self.engine.request_dir('offline-1'), {'status': 'test'})
        self.assertIn('fsync', order[:order.index('atomic')])

    def test_crash_between_audit_and_state_requires_reconciliation(self):
        self.engine.prepare(self.packet)
        original = cf.atomic_write
        def crash(path, data):
            if path.name == 'state.json' and json.loads(data)['status'] == 'builder_started':
                raise OSError('simulated crash after audit fsync')
            return original(path, data)
        with mock.patch.object(cf, 'atomic_write', side_effect=crash):
            with self.assertRaises(OSError):
                self.run_task()
        self.assertEqual(self.run_task()['status'], 'reconciliation_required')
        self.assertFalse((self.workspace / 'build-count').exists())

    def test_production_argv_has_no_approval_sandbox_conflict(self):
        self.patch.stop()
        argv = cf.builder_argv(self.data)
        self.assertEqual(argv[0], str(cf.installed_codex()))
        self.assertIn('--approve-for-me', argv)
        self.assertIn('--json', argv)
        self.assertIn('--cd', argv)
        self.assertEqual(argv[-1], '-')
        for forbidden in ['--sandbox', '--ignore-rules', '--ignore-user-config',
                          '--dangerously-bypass-approvals-and-sandbox']:
            self.assertNotIn(forbidden, argv)
        self.data['profile'] = 'browser-public'
        self.assertIn('browser-public', cf.builder_argv(self.data))
        self.data['profile'] = 'unsafe'
        with self.assertRaises(cf.FallbackError):
            cf.builder_argv(self.data)

    def test_trusted_stages_ignore_workspace_and_pythonpath_modules(self):
        poison = "from pathlib import Path; Path(" + repr(str(self.base / 'poisoned')) + ").write_text('imported'); raise RuntimeError('untrusted import')"
        for name in ['pathlib.py', 'sitecustomize.py']:
            self.builder_code += '; Path(' + repr(name) + ').write_text(' + repr(poison) + ')'
        self.data['verification_argv'] = self.stage(
            "import sys, pathlib; assert sys.flags.isolated and sys.flags.no_site; "
            "assert pathlib.Path.cwd() == pathlib.Path(sys.argv[1]); "
            "assert pathlib.Path(sys.argv[2]).read_text() == 'built'",
            self.base, self.workspace / 'artifact.txt')
        self.save()
        with mock.patch.dict(os.environ, {'PYTHONPATH': str(self.workspace)}):
            self.assertEqual(self.run_task()['status'], 'completed')
        self.assertFalse((self.base / 'poisoned').exists())
        self.assertEqual((self.workspace / 'continued').read_text(), 'once')

    def test_scripts_unisolated_commands_and_workspace_interpreters_are_rejected(self):
        script = self.workspace / 'verifier.py'
        script.write_text('raise SystemExit(0)')
        alias = self.workspace / 'python3'
        alias.symlink_to(cf.TRUSTED_PYTHON)
        invalid = [
            [str(cf.TRUSTED_PYTHON), str(script)],
            [str(cf.TRUSTED_PYTHON), '-c', 'pass'],
            [str(cf.TRUSTED_PYTHON), '-I', '-S', str(script)],
            [str(alias), '-I', '-S', '-c', 'pass'],
        ]
        for key in ['verification_argv', 'continuation_argv']:
            original = self.data[key]
            for argv in invalid:
                with self.subTest(stage=key, argv=argv):
                    self.data[key] = argv
                    self.save()
                    with self.assertRaisesRegex(cf.FallbackError, 'system Python'):
                        self.run_task()
            self.data[key] = original
        self.assertFalse((self.workspace / 'build-count').exists())

    def test_checkpoint_parent_within_workspace_is_rejected(self):
        checkpoint = self.workspace / 'checkpoint'
        checkpoint.write_bytes(self.checkpoint.read_bytes())
        self.data['parent_checkpoint']['path'] = str(checkpoint)
        self.save()
        with self.assertRaisesRegex(cf.FallbackError, 'outside the builder workspace'):
            self.run_task()
        self.assertFalse((self.workspace / 'build-count').exists())

    def test_optional_effort_is_bounded(self):
        self.patch.stop()
        for effort in ['high', 'medium']:
            self.data['reasoning_effort'] = effort
            cf.parse_packet(json.dumps(self.data).encode())
            self.assertIn('model_reasoning_effort="' + effort + '"', cf.builder_argv(self.data))
        for effort in ['low', 'ultra', 'high --ignore-rules']:
            self.data['reasoning_effort'] = effort
            with self.assertRaises(cf.FallbackError):
                cf.parse_packet(json.dumps(self.data).encode())
            with self.assertRaises(cf.FallbackError):
                cf.builder_argv(self.data)


if __name__ == '__main__':
    unittest.main()
