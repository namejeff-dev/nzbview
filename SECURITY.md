# Security

This beta supports a single trusted local installation. It defaults to loopback. Network-facing use requires an access token, explicit allowed hosts and TLS when traversing untrusted networks. Keep the backend private when using a reverse proxy. It is not designed as a publicly exposed multi-user service.

Only the latest beta receives fixes. Please use GitHub's private vulnerability reporting for security issues once the repository owner enables it. If that feature is unavailable, contact the repository owner privately; do not post exploitable details or real credentials in a public issue. The repository owner should enable private reporting before publication.

Collector configuration contains powerful service keys. Keep it outside Git or use process environment variables. Raw logs, database rows, diagnostic output and screenshots can retain private metadata. Output redaction is defense in depth, not a replacement for reviewing material before sharing it.

The OS-backed collector lock coordinates writers using this package. External programs can still modify the DB independently. Use local writable storage, appropriate file permissions and verified backups before maintenance.
