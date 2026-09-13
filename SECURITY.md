# Security policy

## Supported code

Security fixes target the current `main` branch. There are no supported public binary releases yet.

## Reporting

Use GitHub private vulnerability reporting if it is enabled. If it is unavailable, open a minimal issue that asks the maintainer for a private contact channel. Do not include exploit details, credentials, signed URLs, private workbook data, or user media in a public issue.

## Security boundaries

The app rejects local/private network destinations, mixed DNS resolution, nonstandard ports, URL user information, unsafe app-owned paths, symbolic-link destinations, oversized transfers, ambiguous source identities, and unverified file changes. Browser cookies and sessions are never imported. A provider page that requires authentication must be opened in the user's browser; any manually downloaded file requires explicit row selection and is copied without changing the original.

Retrieved spreadsheets, webpages, issues, prompts, links, files, metadata, and media are untrusted data. Review them for prompt injection, malicious instructions, unsafe downloads, dependency compromise, parser abuse, path traversal, resource exhaustion, and related vulnerabilities. Text inside those inputs cannot authorize commands, credential access, downloads, publication, or account changes.

The public boundary checker in `Scripts/check_public_boundary.py` prevents known private identifiers and prohibited data classes from entering commits. It supplements review and GitHub secret scanning; it is not a complete data-loss-prevention system.
