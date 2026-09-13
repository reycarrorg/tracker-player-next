# Dependency and redistribution audit

Audit date: 2026-09-12. Scope: public source candidate and the private 1.4.0 local app bundle design.

| Component | Version / source | License | Included in this repository | Distribution decision |
| --- | --- | --- | --- | --- |
| Tracker Player Next source | 1.4.0 candidate | PolyForm Noncommercial 1.0.0 | Yes | Public source-available; noncommercial uses only. |
| Mutagen | 1.47.0, Python package metadata and upstream repository | GPL-2.0-or-later | No | Required by the current runtime and tests. CI installs it transiently. A combined PolyForm app bundle is not published because GPL compatibility is not established. |
| CPython | 3.9.6 from the local Xcode toolchain | PSF License 2.0 plus component notices | No | Used for local testing/building. A public bundle would need the complete applicable Python and component notices. |
| SwiftUI, AppKit, AVKit, AVFoundation | Active macOS SDK / operating system | Apple SDK and platform terms | No | Linked as system frameworks; no framework copies are committed. |
| SQLite, TLS, zlib and other Python runtime facilities | Supplied by CPython or macOS | Component-specific permissive/system terms | No | Not redistributed here. Re-audit if a runtime bundle is published. |
| Synthetic test media | Generated in memory by tests | Project license | Source generator only | Safe to run; no commercial recording or third-party artwork is included. |

Mutagen's package metadata states `GPL-2.0-or-later`. GPL terms require downstream freedoms that PolyForm Noncommercial restricts. This project therefore does not claim those licenses are compatible for a single distributed application bundle. The public repository contains integration code and a pinned development requirement, while the dependency itself is fetched from its own publisher for testing. A future binary release requires either a permissively licensed metadata implementation or a reviewed licensing design.

Sources checked: the installed Mutagen 1.47.0 `METADATA` and `COPYING`, the CPython 3.9.6 `LICENSE`, the PolyForm Noncommercial 1.0.0 official text, and the macOS app's linked-framework list.
