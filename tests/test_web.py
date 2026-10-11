import importlib
from types import SimpleNamespace

import pytest

import database_collector as collector


def initialize(web, configured):
    app, client = web
    conn = collector.open_db(configured[2])
    conn.commit()
    conn.close()
    return app, client


def test_missing_database_shows_setup(web):
    _, client = web
    response = client.get('/')
    assert response.status_code == 200
    assert 'Welcome to nzbview' in response.text
    assert 'collector_config.example.json' in response.text


@pytest.mark.parametrize('route', ['/', '/activity', '/indexers', '/raw-search-results', '/providers', '/downloads',
                                   '/releases', '/recovered', '/failures', '/diagnostics', '/help'])
def test_pages_render_with_empty_database_in_full_and_partial_modes(web, configured, route):
    _, client = initialize(web, configured)
    full = client.get(route)
    assert full.status_code == 200
    assert 'Traceback' not in full.text
    partial = client.get(route, headers={'HX-Request': 'true'})
    assert partial.status_code == 200
    assert '<!doctype html>' not in partial.text.lower()


@pytest.mark.parametrize('population', ['searches', 'grabs', 'downloads'])
def test_sample_pages_handle_empty_data(web, configured, population):
    _, client = initialize(web, configured)
    for route in ['/indexers', '/providers', '/downloads', '/raw-search-results']:
        response = client.get(route, params={'scope': 'sample', 'sample_type': population, 'sample_n': 100})
        assert response.status_code == 200


def test_time_windows_expire_without_database_write(web, monkeypatch):
    app, _ = web
    clock = [0]
    monkeypatch.setattr(app, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(app, '_db_generation', lambda: 123)
    seen = []
    monkeypatch.setattr(app.db, 'snapshot', lambda hours: seen.append(hours) or {'sequence': len(seen)})
    app._SNAP_CACHE.clear()
    assert app.get_snap(24)['sequence'] == 1
    clock[0] = 30
    assert app.get_snap(24)['sequence'] == 1
    clock[0] = 61
    assert app.get_snap(24)['sequence'] == 2
    assert len(app._SNAP_CACHE) == 1


def test_post_requires_same_origin_or_explicit_nonbrowser_header(web, monkeypatch):
    app, client = web
    monkeypatch.setattr(app.collector_runner, 'start', lambda: {'running': True})
    assert client.post('/api/collector/run').status_code == 403
    assert client.post('/api/collector/run', headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.post('/api/collector/run', headers={'Origin': 'http://testserver'}).status_code == 202
    assert client.post('/api/collector/run', headers={'X-Nzbview-Request': '1'}).status_code == 202


def test_host_header_is_validated(web):
    _, client = web
    assert client.get('/', headers={'Host': 'evil.example'}).status_code == 400


def test_remote_requests_require_token(web):
    app, _ = web
    from fastapi.testclient import TestClient
    with TestClient(app.app, client=('192.0.2.10', 50000)) as client:
        assert client.get('/').status_code == 403


def test_authenticated_remote_login_and_collector(configured, monkeypatch):
    token = 'test-token-longer-than-24-characters'
    monkeypatch.setenv('NZBVIEW_ACCESS_TOKEN', token)
    import app
    app = importlib.reload(app)
    from fastapi.testclient import TestClient
    monkeypatch.setattr(app.collector_runner, 'start', lambda: {'running': True})
    with TestClient(app.app, client=('192.0.2.10', 50000)) as client:
        assert client.get('/api/collector/status').status_code == 401
        assert client.post('/login', data={'token': 'wrong'}, headers={'Origin': 'http://testserver'}).status_code == 401
        response = client.post('/login', data={'token': token}, headers={'Origin': 'http://testserver'}, follow_redirects=False)
        assert response.status_code == 303
        assert 'HttpOnly' in response.headers['set-cookie']
        assert 'nzbview_session' in client.cookies
        assert client.get('/').status_code == 200
        assert client.post('/api/collector/run', headers={'X-Nzbview-Request': '1'}).status_code == 202
        monkeypatch.setenv('NZBVIEW_ACCESS_TOKEN', 'rotated-token-longer-than-24-characters')
        assert client.get('/api/collector/status').status_code == 401
        assert client.get('/api/collector/status', headers={'Authorization': 'Bearer rotated-token-longer-than-24-characters'}).status_code == 200


def test_local_assets_do_not_require_cdn(web, configured):
    _, client = initialize(web, configured)
    response = client.get('/')
    assert 'unpkg.com' not in response.text
    assert client.get('/static/vendor/htmx-2.0.5.min.js').status_code == 200


def test_health_does_not_disclose_database_path(web, configured):
    _, client = initialize(web, configured)
    response = client.get('/api/health')
    assert response.status_code == 200
    assert str(configured[2]) not in response.text
