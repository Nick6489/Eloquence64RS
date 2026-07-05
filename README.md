# Eloquence64RS for NVDA

Eloquence64RS is an Eloquence synthesizer add-on for 64-bit NVDA. It runs the
legacy 32-bit Eloquence engine in a small Rust host process and streams speech
back to NVDA.

## Project status

This is an unofficial, unsupported, community-maintained add-on, provided as
is and used at your own risk. Release packages include legacy proprietary
Eloquence binaries. Eloquence64RS replaces Eloquence64 and should not be
installed alongside it or the IBMTTS add-on.

The internal NVDA add-on ID remains `Eloquence` so an Eloquence64RS update
replaces an earlier Eloquence64 installation instead of creating a conflicting
side-by-side copy.

All Eloquence64RS release versions contain the `RS` marker. Release candidates
use versions such as `19.0-RS-RC1`; the corresponding stable release is
`19.0-RS`.

## Acknowledgements

I am standing on the shoulders of giants here, taking logic from
[Fastfinge/Eloquence64](https://github.com/fastfinge/eloquence_64) and using
[davidacm/ibmtts-host32-bridge](https://github.com/davidacm/ibmtts-host32-bridge)
as a parts bin to create a best-of-both-worlds Eloquence. Thanks to those
project maintainers and contributors, without whom this would not exist.

## 64-bit support

The Eloquence DLL is 32-bit only. The add-on therefore ships a small 32-bit
Rust Host Process, `synthDrivers/eloquence_host32.exe`. NVDA's in-process Python
synth driver launches it and exchanges authenticated, versioned frames over the
child's standard-input and standard-output pipes. No helper interpreter,
listening socket, or host-selection environment variable is used.

The host creates a private staged copy of the Eloquence DLL and `ECI.INI` for
each run. It re-anchors `Path=` and `Path_Rom=` entries in that private copy, so
the installed configuration is never rewritten. If the executable cannot
start, authenticate, or initialize ECI, synth initialization fails with the
cause logged; NVDA can then select its next available synthesizer.

## Traditional Chinese Script Conversion

When the Mandarin Chinese voice is selected, text preprocessing converts
Traditional Chinese text to Simplified Chinese before sending it to Eloquence.
This supplies Mandarin readings; it is not a Traditional Chinese voice or
Cantonese support. The add-on advertises only `zh-CN`.

Known limitations:

- Hong Kong (`zh-HK`) users get Mandarin readings, not Cantonese.
- Colloquial written-Cantonese characters may be unpronounceable.
- A zh-TW-localized NVDA install does not auto-select the Chinese voice on first
  run; select the Chinese voice once manually.

## Eloquence on secure screens

NVDA does not copy executables to its secure-screen configuration. After using
NVDA's **Use currently saved settings during sign-in** command:

1. Open **NVDA Settings > Eloquence**.
2. Choose **Copy Helper to System Config (for Logon Screen)**.
3. Accept the UAC prompt.

Repeat this after each add-on update so the secure-screen copy of the native
host matches the installed add-on.

## Building

Prerequisites are 64-bit Python 3.13, [uv](https://docs.astral.sh/uv/), a stable
Rust MSVC toolchain, and its `i686-pc-windows-msvc` target:

```powershell
rustup target add i686-pc-windows-msvc
git submodule update --init
python fetch_eci.py
build_host.cmd
scons.bat
```

`build_host.cmd` builds the statically linked i686 Rust release executable and
copies it to `addon/synthDrivers/eloquence_host32.exe`. `scons.bat` validates
that this executable and the proprietary Eloquence files exist before creating
the `.nvda-addon` package.

Development checks:

```powershell
runlint.bat
runpytest.bat
cargo fmt --manifest-path native_host/Cargo.toml --check
cargo test --manifest-path native_host/Cargo.toml
cargo clippy --manifest-path native_host/Cargo.toml --all-targets -- -D warnings
```

Real ECI and child-process tests require the i686 target and
`ELOQUENCE_ECI_PATH` pointing to `ECI.DLL`. This variable is test input only; it
does not alter host selection in the packaged add-on.

## Troubleshooting upgrades

If an upgrade leaves stale add-on files, disable Eloquence, restart NVDA, remove
the old `Eloquence` and any `Eloquence.delete` directories under
`%APPDATA%\nvda\addons`, then install the current package cleanly. Running the
IBMTTS and Eloquence add-ons together is unsupported. See upstream
[issue #101](https://github.com/fastfinge/eloquence_64/issues/101) for background.
