import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import database_collector as collector
from db import MonitorDB


@pytest.mark.parametrize('kind', ['sab', 'prowlarr', 'sonarr', 'radarr'])
def test_split_line_is_retained_and_rotation_does_not_duplicate(tmp_path, kind):
    logs = tmp_path / 'logs'
    logs.mkdir()
    if kind == 'sab':
        name = 'sabnzbd.log'
        header = '2026-10-10 20:00:00,000::INFO::[test:1] Header\n'
        line = '2026-10-10 20:00:01,000::INFO::[test:1] Sending notification: Download Completed - Example.S01E01 (type=complete)\n'
        table = 'jobs'
        parse = lambda conn: collector.parse_sab_log_family(conn, logs)
        rotated = 'sabnzbd.log.1'
    elif kind == 'prowlarr':
        name = 'prowlarr.debug.txt'
        header = '2026-10-10 20:00:00.0|Info|Unrelated|Header\n'
        line = '2026-10-10 20:00:01.0|Info|ReleaseSearchService|Searching indexer(s): [ExampleIndexer] for Term: [Example]\n'
        table = 'prowlarr_log_searches'
        parse = lambda conn: collector.parse_prowlarr_log_family(conn, logs, ['ExampleIndexer'])
        rotated = 'prowlarr.debug.0.txt'
    else:
        name = f'{kind}.debug.txt'
        header = '2026-10-10 20:00:00.0|Info|Unrelated|Header\n'
        line = "2026-10-10 20:00:01.0|Debug|TrackedDownloadService|Tracking 'SABnzbd:Example.S01E01': ClientState=Failed, test\n"
        table = 'arr_events'
        parse = lambda conn: collector.parse_arr_log_family(conn, logs, kind)
        rotated = f'{kind}.debug.0.txt'
    path = logs / name
    path.write_text(header + line[:65])
    conn = collector.open_db(tmp_path / 'test.db')
    try:
        with conn:
            parse(conn)
        assert conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 0
        with path.open('a') as stream:
            stream.write(line[65:])
        with conn:
            parse(conn)
        assert conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 1
        path.rename(logs / rotated)
        path.write_text(header.replace('20:00:00', '21:00:00'))
        with conn:
            parse(conn)
        assert conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 1
    finally:
        conn.close()


def test_timezone_uses_log_zone_not_machine_zone(monkeypatch):
    monkeypatch.setenv('NZBVIEW_LOG_TIMEZONE', 'Europe/Berlin')
    parsed = collector.parse_timestamp_any('2026-10-10 20:00:00.0')
    assert parsed == '2026-10-10T20:00:00.000+02:00'
    assert collector.parse_timestamp_any('2026-10-10T18:00:00Z') == parsed
    assert collector.parse_timestamp_any('2026-01-10 20:00:00.0').endswith('+01:00')


def test_upgrade_adds_source_status_without_losing_data(tmp_path):
    db = tmp_path / 'old.db'
    conn = collector.open_db(db)
    with conn:
        collector.insert_sab_job(conn, '2026-10-10T20:00:00+02:00', 'Example', 'completed')
        conn.execute('ALTER TABLE collector_runs DROP COLUMN source_errors_json')
    conn.close()
    conn = collector.open_db(db)
    conn.commit()
    assert conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
    assert 'source_errors_json' in {r['name'] for r in conn.execute('PRAGMA table_info(collector_runs)')}
    conn.close()


def test_api_outage_still_imports_logs_and_records_partial_status(tmp_path, monkeypatch):
    logs = tmp_path / 'logs'
    logs.mkdir()
    (logs / 'sabnzbd.log').write_text('2026-10-10 20:00:01,000::INFO::[test:1] Sending notification: Download Completed - Example (type=complete)\n')
    (logs / 'prowlarr.txt').write_text('2026-10-10 20:00:00.0|Info|Unrelated|Header\n')
    db = tmp_path / 'partial.db'
    seeded = collector.open_db(db)
    collector.save_prowlarr_indexers(seeded, {1: {'name': 'ExampleIndexer', 'protocol': 'usenet', 'enable': True, 'priority': 7}})
    seeded.commit()
    seeded.close()
    monkeypatch.setattr(collector, 'sab_version', lambda *a: 'test')
    monkeypatch.setattr(collector, 'prowlarr_version', lambda *a: 'test')
    def unavailable(*a, **kw):
        raise RuntimeError('offline')
    monkeypatch.setattr(collector, 'sab_api_get', unavailable)
    monkeypatch.setattr(collector, 'prowlarr_api_get', unavailable)
    args = SimpleNamespace(log=None, log_dir=str(logs), prowlarr_log=None, prowlarr_log_dir=str(logs),
                           sonarr_log=None, sonarr_log_dir=None, radarr_log=None, radarr_log_dir=None,
                           api_key='test-sab', prowlarr_api_key='test-prowlarr', sonarr_api_key=None, radarr_api_key=None,
                           url='http://localhost:8080', prowlarr_url='http://localhost:9696', sonarr_url='', radarr_url='',
                           db=str(db), backfill_all=False, rebuild_search_layer=False, match_window_hours=168,
                           debug_prowlarr=False)
    assert collector._main(args) == 3
    conn = collector.open_db(db)
    assert conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
    assert json.loads(conn.execute('SELECT source_errors_json FROM collector_runs').fetchone()[0])
    stored = conn.execute('SELECT enabled,priority FROM prowlarr_indexers WHERE indexer_id=1').fetchone()
    assert tuple(stored) == (1, 7)
    conn.close()
    assert MonitorDB(db).snapshot(1440)['source_errors']


@pytest.mark.parametrize('mode', ['backfill', 'rebuild'])
def test_maintenance_makes_consistent_backup(tmp_path, mode):
    logs = tmp_path / 'logs'
    logs.mkdir()
    (logs / 'sabnzbd.log').write_text('2026-10-10 20:00:00,000::INFO::[test:1] Header\n')
    (logs / 'prowlarr.txt').write_text('2026-10-10 20:00:00.0|Info|Unrelated|Header\n')
    db = tmp_path / 'test.db'
    conn = collector.open_db(db)
    collector.save_prowlarr_indexers(conn, {1: {'name': 'ExampleIndexer', 'protocol': 'usenet'}})
    conn.commit()
    conn.close()
    if mode == 'backfill':
        code = collector.run_full_historical_backfill(db_path=db, sab_log=logs, prow_log=logs, sonarr_log=None, radarr_log=None)
    else:
        code = collector.run_search_layer_rebuild(db_path=db, prow_log=logs, sonarr_log=None, radarr_log=None)
    assert code == 0
    version = collector.VERSION.replace('.', '_')
    backups = list(tmp_path.glob(f'{db.stem}.pre_v{version}_maintenance_*{db.suffix}'))
    assert len(backups) == 1, 'Maintenance must create one versioned database backup'
    import sqlite3
    backup = sqlite3.connect(backups[0].resolve().as_uri() + '?mode=ro', uri=True)
    try:
        assert backup.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert backup.execute('SELECT name FROM prowlarr_indexers WHERE indexer_id=1').fetchone()[0] == 'ExampleIndexer'
    finally:
        backup.close()
