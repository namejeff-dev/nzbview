import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture
def configured(tmp_path, monkeypatch):
    for key in ('SABNZBD_DB', 'NZBVIEW_ACCESS_TOKEN', 'NZBVIEW_PUBLIC_ORIGIN', 'NZBVIEW_LOG_TIMEZONE',
                'SABNZBD_API_KEY', 'PROWLARR_API_KEY', 'SONARR_API_KEY', 'RADARR_API_KEY'):
        monkeypatch.delenv(key, raising=False)
    for name in ('sab', 'prowlarr', 'sonarr', 'radarr'):
        (tmp_path / name).mkdir()
    data = {
        'script': str(ROOT / 'database_collector.py'), 'db': 'data/test.db',
        'sab_url': 'http://127.0.0.1:8080', 'sab_api_key': 'sab-test-key', 'sab_log_dir': 'sab',
        'prowlarr_url': 'http://127.0.0.1:9696', 'prowlarr_api_key': 'prowlarr-test-key', 'prowlarr_log_dir': 'prowlarr',
        'sonarr_api_key': '', 'radarr_api_key': '', 'log_timezone': 'Europe/Berlin',
    }
    path = tmp_path / 'collector_config.json'
    path.write_text(json.dumps(data))
    monkeypatch.setenv('NZBVIEW_COLLECTOR_CONFIG', str(path))
    monkeypatch.setenv('NZBVIEW_ALLOWED_HOSTS', 'localhost,127.0.0.1,testserver,[::1]')
    return path, data, tmp_path / 'data/test.db'


@pytest.fixture
def web(configured, monkeypatch):
    import app
    app = importlib.reload(app)
    from fastapi.testclient import TestClient
    with TestClient(app.app, client=('127.0.0.1', 50000)) as client:
        yield app, client
