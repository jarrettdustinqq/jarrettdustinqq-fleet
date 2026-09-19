"""Offline owned-process cleanup regressions; no browser or network calls."""
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

spec = importlib.util.spec_from_file_location('probe', Path(__file__).with_name('browser_probe.py'))
probe = importlib.util.module_from_spec(spec)
sys.dont_write_bytecode = True
spec.loader.exec_module(probe)


class CleanupTests(unittest.TestCase):
    def exercise(self, escape):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / 'owned-profile'
            profile.mkdir()
            ready = root / 'ready'
            child_code = (
                'import os,signal,time,pathlib; '
                + ('os.setsid(); ' if escape else '')
                + 'signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                + f'pathlib.Path({str(ready)!r}).write_text(str(os.getpid())); '
                + f'p=pathlib.Path({str(profile / "writing")!r}); '
                + '\nwhile True: p.write_text("alive"); time.sleep(.01)'
            )
            parent_code = (
                'import subprocess,sys,time; '
                + f'subprocess.Popen([sys.executable,"-c",{child_code!r},'
                + f'{("--user-data-dir=" + str(profile)) if escape else "group-only-child"!r}]); time.sleep(60)'
            )
            unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
            parent = subprocess.Popen([sys.executable, '-c', parent_code], start_new_session=True)
            child_pid = None
            try:
                identity = probe.start_time(parent.pid)
                end = time.monotonic() + 3
                while not ready.exists() and time.monotonic() < end:
                    time.sleep(.02)
                self.assertTrue(ready.exists(), 'descendant never became ready')
                child_pid = int(ready.read_text())
                result = probe.cleanup_browser(parent, parent.pid, identity, profile, None, grace=.2, timeout=2)
                self.assertEqual(result['status'], 'passed', result)
                for field in ('browser_exited', 'endpoint_closed', 'descendants_exited', 'profile_removed'):
                    self.assertIs(result[field], True)
                self.assertIsNotNone(parent.returncode)
                self.assertFalse(profile.exists())
                self.assertIsNone(unrelated.poll(), 'unrelated process was terminated')
            finally:
                for pid in (child_pid,):
                    if pid:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                for process in (parent, unrelated):
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=2)

    def test_group_descendant_and_unrelated_survives(self):
        self.exercise(False)

    def test_escaped_profile_descendant_and_unrelated_survives(self):
        self.exercise(True)

    def test_deadline_keeps_profile_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / 'profile'
            profile.mkdir()
            process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
            try:
                result = probe.cleanup_browser(process, process.pid, probe.start_time(process.pid),
                                               profile, None, grace=0, timeout=0)
                self.assertEqual(result['status'], 'failed')
                self.assertFalse(result['descendants_exited'])
                self.assertFalse(result['profile_removed'])
                self.assertTrue(profile.exists())
            finally:
                process.kill()
                process.wait(timeout=2)


if __name__ == '__main__':
    unittest.main()
