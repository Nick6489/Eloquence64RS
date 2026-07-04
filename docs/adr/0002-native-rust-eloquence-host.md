# Replace the Python Eloquence Host Process with an opt-in Rust host

Status: Proposed

## Context

The 32-bit Eloquence Engine cannot be loaded into 64-bit NVDA. The current add-on solves this with a
32-bit Python process packaged by PyInstaller. That implementation is the behavioral reference for
engine initialization, Voice Parameters, Pronunciation Dictionaries, audio callbacks, Speech Indexes,
cancellation, and completion recovery.

The Python host works, but it has several structural costs:

- it bundles an interpreter solely to host one native DLL;
- its Pickle-based Host Channel is Python-specific;
- `eciSynchronize` blocks the same controller that receives `stop`;
- audio and progress events cross several queues without an explicit wire contract;
- the packaged executable requires special handling on NVDA Secure Screens.

The separate `ibmtts-host32-bridge` project demonstrates that the ECI ABI can be hosted in native Rust.
Its low-level ECI definitions are useful reference material, but its global pipe names, shared-memory
handshake, thread-local engine state, multi-client design, and IBM-specific behavior are not adopted.

## Decision

Add a new, opt-in 32-bit Rust Eloquence Host Process. During development the existing Python host
remains the default and the behavioral oracle. Removal of the Python host requires explicit parity and
live NVDA validation; it is not part of the initial migration.

The native host will be a single-client process with these boundaries:

1. The existing Synth Driver retains Text Preprocessing, Voice Identity selection, Pause Policy, saved
   Voice Parameter behavior, NVDA settings, and Audio Playback Pipeline integration.
2. The Rust host owns the Eloquence Engine handle, Pronunciation Dictionary handles, output buffer,
   ECI callback, pending Speech Indexes, synthesis lifecycle, and completion recovery.
3. A versioned binary Host Channel replaces Pickle for the native-host path.
4. Control responses and ordered engine events initially use the same framed byte stream. Eloquence
   produces roughly 22 KiB/s of PCM, so shared memory is deferred until benchmarks demonstrate value.
5. `eciSynthesize` and `eciSynchronize` run on a synthesis thread. The control path remains able to
   request `eciStop` while synchronization is in progress.

## Protocol v1

Each transport message contains one complete frame. Integers are little-endian.

| Offset | Size | Field |
| --- | ---: | --- |
| 0 | 4 | ASCII magic `ELQH` |
| 4 | 2 | protocol version (`1`) |
| 6 | 2 | message kind |
| 8 | 4 | request ID (`0` for unsolicited events) |
| 12 | 4 | flags |
| 16 | 4 | payload length |
| 20 | N | payload |

Variable byte strings are encoded as a little-endian `u32` length followed by exactly that many bytes.
Text used by the protocol itself is UTF-8. Eloquence Text remains pre-encoded engine bytes and is never
implicitly transcoded by the host.

The first frame must be `Hello` and must contain the random 16-byte authentication key passed when the
host was launched. Frames are rejected before authentication, on unknown versions, on unknown message
kinds, or when their declared payload exceeds the protocol limit.

Every Speech Generation has a `u64` identifier supplied by the client. Audio, Speech Index, completion,
and stopped events repeat that identifier so stale events can be discarded after cancellation.

## Concurrency and audio invariants

- ECI callbacks never call NVDA or `WavePlayer` directly.
- Waveform, Speech Index, and completion events retain engine callback order.
- A Speech Index is reported to NVDA only after all preceding audio has completed playback.
- Completion is reported exactly once for each accepted Speech Generation.
- If Eloquence completes without invoking an inserted Speech Index callback, the host reports the latest
  pending Speech Index before completion. This preserves the recovery required for issue #111.
- Cancellation invalidates the Speech Generation, clears pending indexes, calls `eciStop`, and prevents
  stale completion from advancing NVDA.
- The host uses bounded queues. A slow or disconnected client cannot grow host memory without limit.
- Panics and foreign callback failures are contained within the host process and become diagnostic events
  where possible; no unwind may cross the ECI callback ABI.

## Lifecycle and security

- One host is launched for one NVDA process; no global singleton is required.
- Pipe names are unique per launch and are passed explicitly to the host.
- The Host Channel is authenticated with a random per-launch key and Windows objects receive restrictive
  security descriptors before the native host becomes the default.
- The host waits on a real parent-process handle rather than polling a PID, avoiding PID-reuse races.
- Engine, dictionary, mapping, event, and process handles have deterministic cleanup paths.

## Migration milestones

1. Protocol codec and golden-vector tests on all development architectures.
2. ECI loader and fake-engine tests for initialization, parameters, dictionaries, callbacks, and cleanup.
3. Standalone 32-bit host integration test with the proprietary engine.
4. Opt-in native client selected by an environment variable; Python host remains the fallback.
5. Live parity testing for Say-All Reading, rapid characters, cancellation, language changes, Asian text,
   Pronunciation Dictionaries, device changes, portable NVDA, and Secure Screens.
6. Startup time, first-audio latency, cancellation latency, memory, and CPU comparison.
7. A separate decision on making the native host default and later removing the Python host.

## Consequences

The project gains a native host without coupling the migration to an immediate replacement. The explicit
protocol and fake-engine boundary make host behavior testable independently of NVDA and the proprietary
engine. During migration, two host implementations must be maintained and parity failures must be fixed in
the native path rather than papered over by changing established Synth Driver behavior.
