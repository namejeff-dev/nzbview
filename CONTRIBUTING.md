# Contributing

Use Python 3.11+ and install `requirements-dev.txt` in a virtual environment. Run the tests, JavaScript syntax checks, dependency audit and release check described in README.md before proposing a change.

For parser/attribution changes, add a small **synthetic or carefully sanitized** fixture showing the behavior being fixed. Include expected populations and reasons; do not turn missing evidence into failure or correlation into certainty.

For bugs, include nzbview/collector versions, Python/OS and upstream service versions, what happened, what you expected, and sanitized reproduction steps. Remove API keys, auth headers, private URLs, usernames and release names from attachments. Keep your local `collector_config.json`, `.env` and database out of Git.

The collector lives in `database_collector.py`; `VERSION` in that module identifies its runtime version independently of the dashboard version. Keep database migrations additive and exercise old-DB preservation. Update CHANGELOG.md for behavior changes.
