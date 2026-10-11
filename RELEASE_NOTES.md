

## Changes

- Portable config templates and consistent relative path resolution; no personal runtime configuration shipped.
- First-run setup with initial collection, optional offline DB initialization, Linux launcher and Docker examples.
- Strict setup validation, environment-based key transfer, diagnostic output redaction.
- Atomic collector startup, shared process lock and reliable failure/timeout status cleanup.
- Complete-line parser offsets; unfinished trailing log lines are revisited on the next run.
- Analytics caches expire within 60 seconds and invalidate on database changes.
- Configured attribution window used across time views, recent samples and release details.
- API failures preserve log ingestion, return exit code 3, and appear in Diagnostics.
- Optional token login for network deployments, explicit allowed hosts and same-origin write checks.
- Locally bundled HTMX, pinned dependencies, regression tests and GitHub CI.
- MIT license, contributing/security/publishing documentation and third-party notices.
- The original SABnzbd, Prowlarr, Radarr and Sonarr icons are restored in 2.15.1.

## Verification for 2.15.2

- 47 regression tests passed on Linux/Python 3.12 after the rename.
- Collector CLI help and release-file checks passed.


## Previous verification on 2026-10-10

- 47 regression tests passed on Linux with Python 3.12.
- Python module compilation and all three JavaScript syntax checks passed.
- Full and HTMX responses tested for every primary dashboard route, plus recent-sample pages.
- Missing-DB setup, API authentication, token rotation, host/Origin validation and remote collector access tested.
- Maintenance creates consistent backups; schema migration preserves existing business data.
- Incremental complete/split-line parsing and rotation deduplication tested for SABnzbd, Prowlarr, Sonarr and Radarr.
- Updated parser produced identical business row counts on retained excerpts from the supplied logs: 3 jobs, 37 SAB events, 15 Arr events, 213 search candidates.
- Pinned runtime dependency audit reported no known vulnerabilities.
- Release-file check passed. This check is deliberately narrow and does not replace manual review or general-purpose secret scanning.

Browser rendering was not verified: the browser was absent and its download failed in this environment. Docker was unavailable, so the image/Compose examples were not executed. Windows and additional Python versions are configured for GitHub CI, not locally verified. TestClient currently emits one upstream deprecation warning about its httpx compatibility path; the HTTP tests pass.

Upstream live APIs were mocked for outage regression tests. No collector requests were sent to the user's running stack during preparation. The supported upstream application-version matrix remains a beta limitation.
