import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from config import collector_command, database_path, load_config
from collector_runner import CollectorRunner
from runtime import collection_lock, redact


def test_relative_paths_ignore_working_directory(configured, tmp_path, monkeypatch):
    path, data, db = configured
    other = tmp_path / 'elsewhere'
    other.mkdir()
    monkeypatch.chdir(other)
    cfg = load_config(path, db)
    assert cfg['db'] == str(db)
    assert cfg['sab_log_dir'] == str(path.parent / 'sab')
    assert database_path(path) == db


@pytest.mark.parametrize('value', ['API_KEY', 'YOUR_SAB_API_KEY', 'CHANGEME', ''])
def test_reject_placeholder_keys(configured, value):
    path, data, db = configured
    data['sab_api_key'] = value
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='sab_api_key'):
        load_config(path, db)


@pytest.mark.parametrize('key,value', [('match_window_hours', '168'), ('timeout_seconds', 0), ('timeout_seconds', True)])
def test_validate_numeric_settings_before_start(configured, key, value):
    path, data, db = configured
    data[key] = value
    path.write_text(json.dumps(data))
    runner = CollectorRunner(path, db)
    with pytest.raises(ValueError, match=key):
        runner.start()
    assert runner.status()['running'] is False


def test_reject_mismatched_database(configured):
    path, data, db = configured
    with pytest.raises(ValueError, match='does not match'):
        load_config(path, db.with_name('other.db'))


def test_environment_keys_not_exposed_in_process_arguments(configured, monkeypatch):
    path, data, db = configured
    monkeypatch.setenv('SABNZBD_API_KEY', 'environment-secret')
    cfg = load_config(path, db)
    cmd, env = collector_command(cfg)
    assert env['SABNZBD_API_KEY'] == 'environment-secret'
    assert all('environment-secret' not in arg and 'test-key' not in arg for arg in cmd)
    assert '--api-key' not in cmd
    assert env['NZBVIEW_LOG_TIMEZONE'] == 'Europe/Berlin'


def test_simultaneous_start_launches_only_once(configured, monkeypatch):
    path, data, db = configured
    runner = CollectorRunner(path, db)
    barrier = threading.Barrier(2)
    actual = runner._load_config
    def load():
        cfg = actual()
        barrier.wait(timeout=5)
        return cfg
    monkeypatch.setattr(runner, '_load_config', load)
    calls = []
    monkeypatch.setattr(runner, '_worker', lambda cfg: calls.append(1))
    monkeypatch.setattr(runner, 'status', lambda: {})
    threads = [threading.Thread(target=runner.start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert len(calls) == 1


def test_worker_command_failure_clears_running(configured, monkeypatch):
    path, data, db = configured
    runner = CollectorRunner(path, db)
    runner._state['running'] = True
    def fail(cfg):
        raise ValueError('command failed')
    monkeypatch.setattr('collector_runner.collector_command', fail)
    runner._worker(load_config(path, db))
    assert runner.status()['running'] is False
    assert runner.status()['status'] == 'error'


def test_worker_redacts_output_and_reports_partial(configured, monkeypatch):
    path, data, db = configured
    runner = CollectorRunner(path, db)
    monkeypatch.setattr('collector_runner.subprocess.run', lambda *a, **kw: SimpleNamespace(
        stdout='sab-test-key', stderr='apikey=another-secret', returncode=3))
    runner._worker(load_config(path, db))
    state = runner.status()
    assert state['status'] == 'partial'
    assert 'sab-test-key' not in state['output_tail']
    assert 'another-secret' not in state['output_tail']


def test_process_lock_prevents_other_process(configured):
    _, _, db = configured
    code = 'from pathlib import Path; from runtime import collection_lock; import sys\nwith collection_lock(Path(sys.argv[1])):\n print("ready", flush=True)\n sys.stdin.read()\n'
    child = subprocess.Popen([sys.executable, '-c', code, str(db)],
                             cwd=str(Path(__file__).resolve().parents[1]),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'ready'
        with pytest.raises(RuntimeError, match='Another collector'):
            with collection_lock(db):
                pass
    finally:
        child.communicate('', timeout=5)
    with collection_lock(db):
        pass


def test_missing_database_field_has_shared_default(configured):
    path, data, db = configured
    del data['db']
    path.write_text(json.dumps(data))
    assert load_config(path, maintenance=True)['db'] == str(path.parent / 'data/database_collector.db')


def test_invalid_timezone_and_extra_arguments(configured):
    path, data, db = configured
    for change, match in [({'log_timezone': 'Unknown/Zone'}, 'log_timezone'), ({'extra_args': ['--db', 'other.db']}, 'extra_args')]:
        path.write_text(json.dumps(dict(data, **change)))
        with pytest.raises(ValueError, match=match):
            load_config(path, db)
