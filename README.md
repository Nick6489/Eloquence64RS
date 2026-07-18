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

## Important upgrade step for secure screens

If you upgrade from Eloquence64, you **must copy the new host executable to
NVDA's secure-screen configuration again**, even if you previously copied the
old helper. The Eloquence64RS host protocol is deliberately incompatible with
the earlier Eloquence64 host. Until the new executable is copied, Eloquence
will fail to load on the logon screen, UAC prompts, and other secure screens.

After installing and restarting NVDA, open **NVDA Settings > Eloquence** and
choose **Copy Helper to System Config (for Logon Screen)**.

All Eloquence64RS release versions contain the `RS` marker. Beta builds use
versions such as `19.1-RS-beta1`, release candidates use versions such as
`19.1-RS-RC1`, and the corresponding stable release is `19.1-RS`.

The built-in add-on updater reads `update.json` only from the repository's
`stable` branch. Work on the development branch, including prereleases, is not
offered to installed users until that production manifest is deliberately
updated on `stable`.

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

## Audio quality

Eloquence produces mono 16-bit PCM at 11,025 Hz. The **Audio quality** synth
setting offers two output modes:

- **Standard 11 kHz** passes the engine PCM through unchanged and is the
  default.
- **Enhanced 22 kHz** applies an original, stateful 2x interpolation, a subtle
  vocal-body lift, and gentle high-frequency emphasis in the Rust host before
  playback.

Enhanced mode is an optional tonal treatment, not additional source bandwidth.
Changing the mode safely cancels current speech, resets the audio processor,
and recreates NVDA's `WavePlayer` with the matching sample rate.

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

NVDA does not copy executables to its secure-screen configuration. An existing
Eloquence64 helper is not compatible with Eloquence64RS and must be replaced.
After using NVDA's **Use currently saved settings during sign-in** command:

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
