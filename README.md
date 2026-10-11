# nzbview

A self-hosted dashboard for SABnzbd, Prowlarr, Sonarr and Radarr. Follow downloads from search and grab through retries, completion and import; compare indexer evidence and provider availability using retained API history and rotating logs.

**Version 2.15.2 — public beta.** FastAPI + Jinja + HTMX, plain JavaScript charts, and a local SQLite database. Collection is a one-shot command, launched manually or by your scheduler. There is no automatic polling of the upstream services when the web app starts.

## What it shows

- Dashboard trends, download attempts, media/season acquisition groups and release journeys.
- Indexer outcomes and raw search evidence, with explicit target-quality and attribution confidence.
- Provider article availability and observed contribution, kept separate from release success.
- Recovery, cancellation, import, infrastructure and failure-stage evidence.
- Diagnostics for rotating-log coverage and unavailable API sources.

Unknown evidence stays unknown. Time correlation alone does not prove causation. Cancellations and post-download import errors are not silently treated as indexer-quality failures. Percentages use the populations described in the UI and Help.

## Requirements

- Python **3.11 or newer**. Python 3.12 on Linux was verified for this release; GitHub CI covers 3.11–3.13 on Linux and Windows once the repository is published.
- SABnzbd and Prowlarr API access, plus read access to their log directories.
- Optional Sonarr/Radarr log directories. Optional Sonarr/Radarr API keys improve quality-profile target snapshots.
- A writable database directory. Keep SQLite on local storage, with one collector and one web process per database.
- DEBUG logs provide richer search, retry and import evidence. Normal INFO logs still work, with reduced detail. TRACE logs are not collected.

The exact upstream application versions supported are not yet exhaustively established. This beta uses SABnzbd's `/api`, Prowlarr's `/api/v1`, and Sonarr/Radarr's `/api/v3`, together with the rotating text-log formats covered by the parser. Include your app versions in sanitized bug reports.

## Install on Windows

Extract the folder, open PowerShell there, and run:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item collector_config.example.json collector_config.json
```

Edit `collector_config.json`, then start `Start nzbview.bat` or run:

```powershell
.\.venv\Scripts\python.exe run.py
```

Open <http://127.0.0.1:8787>. The setup page explains the missing configuration/database and offers initial collection when your configuration is valid. Keep the server console open.

To collect from the terminal:

```powershell
.\.venv\Scripts\python.exe collect.py
```

## Install on Linux

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp collector_config.example.json collector_config.json
# Edit the copied configuration before collection.
./start.sh
```

To collect once: `.venv/bin/python collect.py`. `python run.py --init-db` can initialize an empty database and start the web app without contacting any service. Normal collection also creates and upgrades the database.

HTMX is bundled locally. Browsing the dashboard requires no CDN connection after dependencies are installed.

## Configure paths and services

All **relative paths in the configuration are relative to the configuration file's directory**, regardless of where you start Python. The shared default database is `data/database_collector.db` next to that configuration. CLI `--db` paths are relative to the command's working directory; an explicit override must agree with the configured `db` field.

The example configuration is a template: replace the required keys, URLs and paths. Use your actual ports; SABnzbd commonly uses 8080 but installations may differ.

| Field | Purpose |
| --- | --- |
| `script` | Collector path; the included relative filename works after moving the folder. |
| `db` | Existing or new SQLite database. Use an absolute path to preserve a DB outside the application folder. |
| `sab_url`, `sab_api_key` | SABnzbd base URL and full API key. |
| `prowlarr_url`, `prowlarr_api_key` | Prowlarr base URL and API key. |
| `sab_log_dir`, `prowlarr_log_dir` | Required readable log directories. Single-file alternatives are `sab_log` and `prowlarr_log`. |
| `sonarr_log_dir`, `radarr_log_dir` | Optional readable log directories. Leave empty to disable. |
| `sonarr_url`/`sonarr_api_key`, `radarr_url`/`radarr_api_key` | Optional profile snapshots. Empty or placeholder optional keys disable API access. |
| `log_timezone` | IANA timezone used by the applications' timezone-less text logs, e.g. `Europe/Berlin` or `UTC`. |
| `match_window_hours` | Attribution look-back, 1–1440 hours; shared across collector and dashboard. Default 168. |
| `timeout_seconds` | Web collector timeout, 1–86400 seconds. Default 900. |

Windows JSON paths must escape backslashes, for example `"C:\\Services\\Sonarr\\logs"`. Linux paths use `/srv/sonarr/config/logs`. Point to the full directory to include rotations: `sabnzbd.log(.N)`, and `prowlarr/sonarr/radarr.txt`, `.N.txt`, `.debug.txt`, `.debug.N.txt`.

When nzbview runs directly on a Linux host and the services run in Docker, configure **host paths** such as `/srv/sonarr/config/logs`. When nzbview also runs in Docker, mount those directories read-only and configure **container paths** such as `/logs/sonarr`. Paths shown inside another service's container are not automatically visible to nzbview.

Set `log_timezone` to the zone used in the source logs, not merely the container's default UTC zone. All configured services should use the same log timezone. For an existing database, keep the original collector's timezone to preserve historical event identities. If source logs use different timezones, align them before collection. Restart the dashboard after changing configuration.

### Keys through environment variables

You can keep JSON key fields empty and set `SABNZBD_API_KEY`, `PROWLARR_API_KEY`, `SONARR_API_KEY`, and `RADARR_API_KEY` in the process environment. Environment values take precedence. The included runner passes keys through the child environment rather than process arguments and redacts configured keys from captured output.

`NZBVIEW_COLLECTOR_CONFIG` selects another configuration file. `SABNZBD_DB` selects a database, subject to the configured-path consistency check. `.env` is read by Docker Compose; direct Python launchers do not load it automatically.

### Collection and scheduling

Run `collect.py` manually, use **Run Collector** in the dashboard, or schedule the command with Windows Task Scheduler/systemd/cron. Use the virtual environment's absolute Python path and the absolute path to `collect.py`; relative config paths still resolve correctly. Pick an interval longer than a typical collection run. The shared OS lock prevents concurrent collection/backfill/rebuild writes.

The frontend's collector status refresh is not upstream API polling. Data changes only when collection runs.

Exit codes: `0` success, `1` runtime failure, `2` invalid CLI/setup, `3` partial collection. If an API is unavailable, log ingestion continues and Diagnostics identifies the affected source. API-derived metrics may then be stale or incomplete. On the very first run, Prowlarr search attribution needs the Usenet indexer list from its API; unavailable indexer metadata limits log attribution.

The API history fetch currently has limits of 5,000 SAB entries per normal/archive view and 50,000 Prowlarr records per run. Retained logs supplement history; this is not a guarantee of complete history beyond what upstream services retain.

### Maintenance and backups

```bash
python backfill_database.py
python rebuild_search_data.py
```

Both accept `--collector-config` and `--db`, share the same lock as normal collection, and create a consistent SQLite backup next to the database before replay/rebuild. Run normal collection first so Prowlarr's Usenet indexer list is known. A backfill replays retained logs; a search rebuild clears and recreates the derived search layer while retaining SAB/history/provider data and exact API targets. Keep sufficient free space for the backup. Stop the dashboard during maintenance to avoid displaying partially rebuilt evidence.

These commands cannot reconstruct events whose logs/history were already deleted by the upstream service. The application currently has no automatic retention/pruning policy; monitor DB/backup growth yourself.

## Docker on Linux

Docker files are included, but the image was not built in the release preparation environment. Treat this deployment path as beta and verify it on your stack.

```bash
cp collector_config.docker.example.json collector_config.json
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# Put the generated token in .env; configure host log paths and service/API settings.
docker compose up --build -d
```

Open <http://127.0.0.1:8787>, sign in with the token, and start collection. Or run `docker compose exec nzbview python collect.py`. The database uses the persistent `nzbview-data` volume. Logs and configuration are read-only mounts. The container runs as UID 10001; ensure this user can traverse/read the mounted log directories without making credentials or logs world-writable.

The example reaches services through their published host ports using `host.docker.internal`. Adjust URLs/ports for your setup. To use Docker service names instead, attach nzbview to the services' shared Docker network. Add Sonarr/Radarr read-only mounts and their `/logs/...` paths if needed.

Keep the localhost port mapping unless remote access is intended. To support LAN access, change the published interface and add the exact server IP/hostname to `NZBVIEW_ALLOWED_HOSTS`. The access token remains required.

## Local and network access

Default binding is `127.0.0.1:8787`. Every non-local installation requires a non-placeholder `NZBVIEW_ACCESS_TOKEN` with at least 24 characters; generate it with `secrets.token_urlsafe(32)`. Put exact hostnames/IPs (without ports) in the comma-separated `NZBVIEW_ALLOWED_HOSTS`; default hosts are localhost and loopback. Do not use a wildcard.

With a token set, browser requests require login and APIs accept `Authorization: Bearer <token>`. Browser sessions expire after 12 hours; rotating the token invalidates existing sessions. Write requests check Origin; non-browser clients must also send `X-Nzbview-Request: 1`. TLS is required for access over untrusted networks. This app is intended for a trusted local environment, not a public multi-user service.

When using a reverse proxy, expose the backend only to that proxy, use HTTPS, preserve the request Host, and set `NZBVIEW_PUBLIC_ORIGIN` to the external origin, such as `https://nzbview.example.net`. Forwarded client-IP headers are not trusted. Hosting below a subpath is not supported; use a dedicated hostname at `/`.

Logs, databases, screenshots and collector output may contain private release names, paths, hosts and URLs even after API-key redaction. Do not publish your real configuration or database.

## Development and publishing

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
node --check static/app.js
node --check static/setup.js
python scripts/check_release.py
python -m pip_audit -r requirements-pinned.txt --disable-pip --no-deps
```

The regression suite covers fresh setup, routes, partial navigation, samples, configuration validation, concurrent starts, process locks, partial log lines, rotation deduplication, timezone conversion, API outages, schema upgrades, maintenance backups and network access controls. Runtime versions are pinned in `requirements-pinned.txt`; update them together with `requirements.in` and rerun tests/audit. Node is needed for JavaScript checks only.

Publish the **contents of this folder** as the repository root. `.github` workflows must be at the root. Keep actual configuration, `.env`, databases, logs, backups and old ZIPs untracked; `.gitignore` includes those exclusions. Run the release check on staged/tracked files and review `git diff --cached`. Enable GitHub secret scanning/push protection where available. Add only sanitized screenshots and small synthetic fixtures. Never attach real debugging logs to a public issue without reviewing them.

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [CHANGELOG.md](CHANGELOG.md), and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## License

Project code is MIT licensed. Bundled third-party components retain their own notices. nzbview is an independent project and is not endorsed by SABnzbd, Prowlarr, Sonarr or Radarr.
