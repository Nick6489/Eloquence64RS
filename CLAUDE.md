# CLAUDE.md

This repository is an NVDA add-on that makes the 32-bit Eloquence Engine
available to 64-bit NVDA. `CONTEXT.md` is the canonical glossary; use
"Eloquence Host Process", "Host Channel", and "Speech Progress Notification"
for those domain concepts.

## Build and checks

```powershell
rustup target add i686-pc-windows-msvc
python fetch_eci.py
build_host.cmd
runlint.bat
runpytest.bat
scons.bat
```

`build_host.cmd` performs a release i686 Rust build and copies the result to
`addon/synthDrivers/eloquence_host32.exe`. There is no 32-bit Python build
environment and no PyInstaller step.

Rust checks:

```powershell
cargo fmt --manifest-path native_host/Cargo.toml --check
cargo test --manifest-path native_host/Cargo.toml
cargo clippy --manifest-path native_host/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path native_host/Cargo.toml --target i686-pc-windows-msvc
```

Set `ELOQUENCE_ECI_PATH` for real-DLL integration tests only.

## Architecture

- `addon/synthDrivers/eloquence.py` is NVDA's in-process Python `SynthDriver`.
  It handles NVDA speech commands, language/voice selection, preprocessing,
  settings, and secure-screen host copying.
- `addon/synthDrivers/_eloquence.py` manages the child lifetime, commands,
  audio playback through `nvwave.WavePlayer`, cancellation generations, and
  NVDA index/completion callbacks.
- `addon/synthDrivers/_eloquence_native.py` implements protocol-v1 framing for
  authenticated inherited stdin/stdout pipes.
- `native_host/` is the sole 32-bit Host Process implementation. It owns ECI,
  dictionaries, callbacks, synthesis synchronization, cancellation, and its
  private staged DLL/INI pair.

The packaged executable is always `synthDrivers/eloquence_host32.exe`. There is
no Python host, socket/pickle transport, runtime switch, or fallback helper. A
startup, authentication, or ECI initialization error terminates the child and
propagates from synth initialization so NVDA may choose another synthesizer.

The host stages `ECI.DLL` and `ECI.INI` in a private temporary directory and
re-anchors `Path=` and `Path_Rom=` there. Never restore code that rewrites the
installed `ECI.INI`.

## Behavioral invariants

- Audio, indexes, and completion preserve Eloquence callback order.
- NVDA receives an index only after preceding audio has played.
- Zero-byte WavePlayer feeds carry index callbacks without audible samples.
- Missing final Eloquence indexes are recovered before completion (issue #111).
- Cancellation advances the speech generation and discards stale events.
- The control path can call `eciStop` while synthesis synchronization runs.
- Host frames are authenticated, bounded, protocol-versioned, and reject
  malformed or oversized input.

Always compare synth-driver behavior with current NVDA `nvwave`, Say All,
speech manager, and speech-without-pauses code and with the NVDA add-on
development documentation listed in `AGENTS.md`.

## Source layout

```text
addon/synthDrivers/eloquence.py          NVDA SynthDriver
addon/synthDrivers/_eloquence.py         client lifecycle and audio pipeline
addon/synthDrivers/_eloquence_native.py  framed protocol client
native_host/src/                         Rust ECI host and protocol server
native_host/tests/host_process.rs        real child/ECI integration
tests/                                   Python client and packaging tests
build_host.cmd                           i686 release build/copy
SConstruct                               validation and add-on packaging
```

Proprietary `ECI.DLL` and western `.SYN` files are ignored. Obtain them with
`python fetch_eci.py`; SCons reports a deterministic error when they are absent.

When adding a Host Command, update both the Python protocol client and Rust
wire/server handling, then test x64 codec behavior, i686 real ECI behavior,
Python client integration, and final package contents.
