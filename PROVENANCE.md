# Source provenance

The public codebase was extracted from the Tracker Player Next development line on 2026-09-12. The last previously package-verified private build was version 1.3.0. Later delivery work added full-era queueing, source-recovery states, deterministic metadata placeholders, stricter tag writing and readback, explicit artwork assignments, transfer progress, and related regression tests.

The publication candidate reconciles that later source rather than presenting 1.3.0 as current. Private catalog adapters, captured data, artwork, historical evidence, user media, and machine-specific build paths were excluded. The public adapter validates the snapshot schema and source URL without embedding the private sheet identity.

Version 1.6.0 identifies the current local application candidate and public source migration. It retains the 1.5.0 source browser, artwork, fallback, and progress behavior and adds an authored provider boundary, bounded Range resume, a WebKit-owned authenticated download handoff, destination-copy verification, and credential-free fixtures. It is not a GitHub binary release or tag.
