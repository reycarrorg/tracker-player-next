# Download architecture — 1.6.0 candidate

## Decision

Tracker Player keeps its authored, DNS-pinned public HTTPS transport behind the
small `DownloadProvider` boundary in `download_providers.py`. Ordinary links are
tried in stable easiest-first order. Interrupted direct transfers make at most
three attempts and use a validated HTTP Range response to resume within that
bounded operation. Existing per-file and batch limits, progress, cancellation,
content sniffing, checksum verification, deterministic names, and zero-link
placeholders remain in force.

When a provider needs a legitimate user login, the native app opens that exact
row link in `WKWebView` with WebKit's persistent app data store. The user enters
their own credentials and activates the provider's own download control. WebKit
saves to a new path chosen in `NSSavePanel`; only the finished local file path is
passed to the existing validation, metadata, artwork, and tracker-identity
pipeline. Python never receives or exports passwords, cookies, tokens, WebKit
storage, or request headers. A completed app-owned download can also be copied to
a new user-selected destination with source and destination checksum readback.

The WebKit session is app-local and persistent for quick later downloads. It is
not shared with Safari or the credential-free public HTTPS provider. This design
does not bypass access controls, CAPTCHA, MFA, provider download restrictions, or
terms. If a site does not expose a legitimate download to the signed-in user, the
row stays unresolved and can retain its source-backed text placeholder.

## Alternatives checked on 2026-09-15

| Project evaluated | Version or channel | License | Decision |
| --- | --- | --- | --- |
| JDownloader 2 | Current rolling JDownloader 2 distribution; the official project does not expose a stable app version on its download page | GPLv3 source terms | Not embedded. It adds a Java/SVN plugin stack and a copyleft distribution boundary much larger than this app's needs. |
| aria2 | 1.37.0 | GPLv2 | Not embedded. It is a capable generic transfer engine, but it does not provide the required in-app authenticated browser session and would add a new GPL combined-distribution question. |
| yt-dlp | 2026.08.19 | Repository and Python package: Unlicense; release binaries have additional third-party terms, including GPLv3+ for PyInstaller builds | Not embedded. Its extractor and browser-cookie workflows are broader than row-attached downloads, change frequently, and conflict with the rule that browser cookies never enter the engine. |
| Apple WebKit | macOS 14 system framework used by the build target | Apple platform framework | Used only for the authored native authentication and download handoff. No upstream WebKit source was copied. |

Upstream references:

- https://jdownloader.org/gpl
- https://support.jdownloader.org/en/knowledgebase/article/setup-ide-eclipse
- https://github.com/aria2/aria2/releases/tag/release-1.37.0
- https://github.com/aria2/aria2/blob/master/COPYING
- https://github.com/yt-dlp/yt-dlp/releases/tag/2026.08.19
- https://github.com/yt-dlp/yt-dlp/blob/master/README.md
- https://developer.apple.com/documentation/webkit/wkwebsitedatastore

No JDownloader, aria2, or yt-dlp code, binaries, plugins, or dependencies were
reused. `AuthenticatedBrowser.swift`, `download_providers.py`, the Range handling,
and their fixtures were authored for Tracker Player. The existing Mutagen 1.47.0
dependency and its already-documented redistribution review remain unchanged.

## Security boundaries

- Production public transfers accept only public HTTPS on port 443, reject URL
  user info, pin validated public DNS results per redirect, and revalidate every
  redirect.
- The in-app top-level browser and download redirects accept HTTPS domain names,
  reject user info, nonstandard ports, localhost, `.local`, and literal IP hosts.
  WebKit subresource networking remains WebKit-controlled.
- Signed query values and session-shaped fields are redacted from events,
  manifests, placeholders, and failed-attempt diagnostics. Recovery preferences
  store only the row's source index, never the raw URL.
- Download names come from tracker metadata. Provider filenames cannot choose an
  app-owned path. External copies require a user-selected new file, reject
  symlinked parents, use no-follow exclusive creation, never extract archives,
  and verify the copied checksum.
- Payload type is detected from bytes. HTML/login responses and executable bytes
  are rejected; archives are never executed or unpacked.

## Canary

1. Choose one tracker row whose link legitimately requires an account the user
   owns, then start its download.
2. In **Resolve source access**, choose **Sign in and download in app**.
3. Sign in directly on the provider page. Complete MFA or CAPTCHA there if the
   provider asks for it.
4. Use the provider's own download button and choose a new temporary filename.
5. Confirm that Tracker Player reports the row completed, shows the file under
   the matching worksheet and era, and that a second visit remains signed in.
6. If the provider opens media without offering a download, return and use
   **Attach a file already downloaded…**. Do not treat browser playback alone as
   a completed transfer.

This canary is the remaining user-authenticated runtime gate. Automated fixtures
use no real account, credential, cookie, private URL, or copyrighted media.
