#!/usr/bin/env python3
"""Owned, public-page CDP recovery measurement; no cloud access."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import time

TARGET = 'https://jarrettdustinqq.github.io/discovery-workbench/'
PLAYWRIGHT = '/home/jarrettdustinqq/agent_workspace/live-browser-mcp-20260918/node_modules/playwright'
CLOUD = dict(status='blocked', reason='access_not_established', attempted=False)


def write(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def start_time(pid):
    return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]


def stop(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


def run(out):
    info = out.stat()
    if not out.is_absolute() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('output must be an existing absolute private directory owned by this user')
    names = ['result.json', 'events.jsonl', 'client-a.json', 'client-b.json', 'before.png', 'after.png',
             'browser.stderr', 'client-a.stderr', 'client-b.stderr']
    if any((out / name).exists() or (out / name).is_symlink() for name in names):
        raise ValueError('existing evidence: reconcile with owner; do not replay into this directory')
    os.umask(0o077)
    began = time.monotonic()
    result = dict(schema_version=1, target=TARGET, local={'status': 'failed'}, cloud=CLOUD.copy(),
                  navigation=dict(status='skipped', http_status=None, title=None, url=None, elapsed_ms=None),
                  recovery={'status': 'skipped'}, cleanup={'status': 'failed', 'browser_exited': False, 'endpoint_closed': False},
                  screenshots={'before': 'before.png', 'after': 'after.png'})
    browser = a = b = None
    profile = port = None
    logs = []

    def event(name, **values):
        with (out / 'events.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(event=name, timestamp=time.time(), elapsed_ms=round((time.monotonic()-began)*1000, 3), **values)) + '\n')

    def launch(argv, name):
        stream = (out / (name + '.stderr')).open('wb')
        logs.append(stream)
        env = dict(os.environ)
        env.pop('NODE_OPTIONS', None)
        env.pop('NODE_PATH', None)
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stream, env=env)

    def checkpoint(process, name, seconds):
        end = time.monotonic() + seconds
        path = out / (name + '.json')
        while time.monotonic() < end:
            if path.exists():
                data = json.loads(path.read_text())
                if name == 'client-a' and 'navigation' in data:
                    result['navigation'] = data['navigation']
                if data.get('status') != 'passed':
                    raise RuntimeError(f'{name}: {data.get("error", "failed")}')
                return data
            if process.poll() is not None:
                raise RuntimeError(f'{name} exited before checkpoint: {process.returncode}')
            time.sleep(0.05)
        raise TimeoutError(name + ' checkpoint deadline')

    def expired(*_):
        raise TimeoutError('whole-run deadline')

    previous = signal.signal(signal.SIGALRM, expired)
    old_term = signal.signal(signal.SIGTERM, expired)
    signal.setitimer(signal.ITIMER_REAL, 78)
    event('supervisor_start', supervisor_pid=os.getpid())
    try:
        profile = Path(tempfile.mkdtemp(prefix='browser-recovery-'))
        browser = launch(['/usr/bin/chromium', '--headless=new', '--remote-debugging-address=127.0.0.1',
                          '--remote-debugging-port=0', '--user-data-dir=' + str(profile),
                          '--no-first-run', '--no-default-browser-check', '--disable-background-networking', 'about:blank'], 'browser')
        end = time.monotonic() + 15
        active = profile / 'DevToolsActivePort'
        while not active.exists():
            if browser.poll() is not None:
                raise RuntimeError('owned Chromium exited during startup')
            if time.monotonic() >= end:
                raise TimeoutError('browser startup deadline')
            time.sleep(0.05)
        lines = active.read_text().splitlines()
        port = int(lines[0])
        if not 0 < port < 65536 or not lines[1].startswith('/devtools/browser/'):
            raise RuntimeError('invalid owned DevTools endpoint')
        endpoint = f'ws://127.0.0.1:{port}{lines[1]}'
        recovery = result['recovery']
        recovery.update(status='failed', browser_pid=browser.pid, browser_start_before=start_time(browser.pid), endpoint_before=endpoint)
        event('browser_started', **recovery)
        command = ['/usr/bin/node', str(Path(__file__).with_name('browser_client.cjs'))]
        a = launch(command + ['a', endpoint, str(out)], 'client-a')
        recovery['client_a_pid'] = a.pid
        result['navigation']['status'] = 'failed'
        first = checkpoint(a, 'client-a', 46)
        result['navigation'] = first['navigation']
        for key in ('endpoint', 'target', 'nonce', 'time_origin'):
            recovery[key + '_before'] = first[key]
        if a.poll() is not None:
            raise RuntimeError('client A did not stay alive')
        event('client_a_checkpoint', client_pid=a.pid, checkpoint=first)
        a.kill()
        recovery['client_a_exit_code'] = a.wait(timeout=2)
        event('client_a_killed', client_pid=a.pid, exit_code=a.returncode)
        if a.returncode != -9 or browser.poll() is not None:
            raise RuntimeError('disconnect did not preserve owned browser')
        recovery['browser_start_after'] = start_time(browser.pid)
        b = launch(command + ['b', endpoint, str(out)], 'client-b')
        second = checkpoint(b, 'client-b', 20)
        if b.wait(timeout=2) != 0:
            raise RuntimeError('client B failed to exit cleanly')
        event('client_b_checkpoint', client_pid=b.pid, checkpoint=second)
        for key in ('endpoint', 'target', 'nonce', 'time_origin'):
            recovery[key + '_before'] = first[key]
            recovery[key + '_after'] = second[key]
            if not first[key] or first[key] != second[key]:
                raise RuntimeError('recovery mismatch: ' + key)
        if recovery['browser_start_before'] != recovery['browser_start_after'] or browser.poll() is not None:
            raise RuntimeError('owned browser identity changed')
        recovery['status'] = 'passed'
    except (Exception, KeyboardInterrupt) as error:
        result['error'] = str(error)
        if result['recovery']['status'] == 'skipped':
            result['recovery']['status'] = 'failed'
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        errors = []
        for process in (a, b, browser):
            try:
                stop(process)
            except Exception as error:
                errors.append(str(error))
        exited = browser is None or browser.poll() is not None
        closed = port is None
        if port is not None:
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=0.5):
                    closed = False
            except ConnectionRefusedError:
                closed = True
            except OSError as error:
                errors.append(str(error))
        try:
            if profile is not None:
                shutil.rmtree(profile)
        except Exception as error:
            errors.append(str(error))
        for stream in logs:
            stream.close()
        result['cleanup'] = dict(status='passed' if exited and closed and not errors else 'failed', browser_exited=exited, endpoint_closed=closed)
        if errors:
            result['cleanup_errors'] = errors
        for name in ('client-a', 'client-b'):
            path = out / (name + '.json')
            if not path.exists():
                write(path, dict(status='skipped', reason='no checkpoint produced'))
        event('cleanup', **result['cleanup'])
        if all(result[key]['status'] == 'passed' for key in ('navigation', 'recovery', 'cleanup')) and 'error' not in result:
            result['local']['status'] = 'passed'
        write(out / 'result.json', result)
        signal.signal(signal.SIGALRM, previous)
        signal.signal(signal.SIGTERM, old_term)
    print(json.dumps(dict(local=result['local'], cloud=CLOUD, cleanup=result['cleanup'], result=str(out / 'result.json'))))
    return 0 if result['local']['status'] == 'passed' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['plan', 'run'])
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.command == 'plan':
        print(json.dumps(dict(executed=False, target=TARGET, local_available=Path('/usr/bin/chromium').is_file() and Path('/usr/bin/node').is_file() and Path(PLAYWRIGHT).is_dir(), cloud=CLOUD)))
    else:
        try:
            if args.output is None:
                raise ValueError('--output is required')
            raise SystemExit(run(args.output))
        except Exception as error:
            print(json.dumps(dict(local={'status': 'failed'}, cloud=CLOUD, error=str(error))))
            raise SystemExit(1)
