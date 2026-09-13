# Public data boundaries

This repository was prepared from a private, historical development workspace. Only original application source, build/test instructions, synthetic tests, and sanitized evidence belong here.

Excluded from the public repository:

- the captured tracker database and workbook exports;
- all real song rows, source URLs, sheet and Drive IDs, and signed query strings;
- downloaded audio/video and the user's local media registry;
- third-party album, era, and song artwork;
- browser cookies, login state, credentials, tokens, and session material;
- QA databases, caches, logs containing private rows, recovery records, and local absolute paths;
- compiled app/ZIP artifacts whose combined dependency redistribution rights are unresolved.

The engine accepts a runtime-supplied, immutable Google Sheets snapshot. The repository does not supply one. Tests construct small synthetic snapshots in temporary directories.

The private product build may inject a catalog and explicitly attributed artwork from outside this repository. That local operation does not authorize publishing or redistributing those inputs.
