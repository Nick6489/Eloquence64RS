# Use the Rust Eloquence Host Process exclusively

Status: Accepted

## Context

ADR 0002 introduced an opt-in Rust host alongside the packaged Python host.
Live NVDA testing established parity for synthesis, index recovery, rapid
characters, cancellation, voice changes, and clean audio markers. Maintaining
two helpers also retained PyInstaller, a 32-bit Python environment, two IPC
protocols, fallback state, and ambiguous failure behavior.

## Decision

The 32-bit Rust host is the only Eloquence Host Process. Its packaged filename
is the established `synthDrivers/eloquence_host32.exe`. The Python host,
pickle/socket protocol, rollout switches, and helper fallback are removed.

The Synth Driver always launches the bundled executable and uses authenticated
framed stdin/stdout protocol v1. Startup, authentication, or ECI initialization
failure is logged and propagated as synth initialization failure; NVDA may then
select another synthesizer.

The host creates a private staged DLL/INI pair and re-anchors `Path=` and
`Path_Rom=` in that copy. Installed add-on files remain unchanged. NVDA playback
continues to use zero-byte WavePlayer feeds for ordered index callbacks.

## Consequences

The add-on ships one small native helper and no helper interpreter. Builds now
require the Rust `i686-pc-windows-msvc` target but no 32-bit Python. CI validates
formatting, Clippy, x64 protocol/unit tests, i686 real-DLL and child-process
tests, Python client tests, and final package contents. Git history remains the
archive for the retired Python implementation.
