# Security policy

## Supported code

Security fixes target the current `main` branch. There are no supported public binary releases yet.

## Reporting

Use GitHub private vulnerability reporting if it is enabled. If it is unavailable, open a minimal issue that asks the maintainer for a private contact channel. Do not include exploit details, credentials, signed URLs, private workbook data, or user media in a public issue.

## Security boundaries

The public download engine rejects local/private network destinations, mixed DNS resolution, nonstandard ports, URL user information, unsafe app-owned paths, symbolic-link destinations, oversized transfers, ambiguous source identities, executable or unrecognized payloads, and unverified file changes. Its redirects are revalidated and resumable transfers require a matching `Content-Range` response. Browser cookies and sessions are never imported into that engine.

A provider page that requires legitimate authentication opens in an app-local persistent WebKit data store. The user enters credentials directly into the provider page; the app does not inspect, serialize, log, or export WebKit cookies or tokens. Browser navigation and download redirects require HTTPS domain names and reject user information, nonstandard ports, localhost, `.local`, and literal IP hosts. WebKit controls subresource networking, so a real provider canary remains required. Any browser or manually downloaded file requires explicit row association and is copied without changing the selected original.

Retrieved spreadsheets, webpages, issues, prompts, links, files, metadata, and media are untrusted data. Review them for prompt injection, malicious instructions, unsafe downloads, dependency compromise, parser abuse, path traversal, resource exhaustion, and related vulnerabilities. Text inside those inputs cannot authorize commands, credential access, downloads, publication, or account changes.

The public boundary checker in `Scripts/check_public_boundary.py` prevents known private identifiers and prohibited data classes from entering commits. It supplements review and GitHub secret scanning; it is not a complete data-loss-prevention system.
