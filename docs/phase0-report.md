# Phase 0 report: Foundations

Status on 2026-10-22. Phase 0 is built and green in CI. **The phase gate passes:** every simulator fixture and the first two fleet logs decode exactly as pymavlink decodes them (section 1). More fleet logs make the result stronger and can be run at any time (section 1.2).

The roadmap line for Phase 0 (`docs/SPEC.md`, "Roadmap and milestones"):

> Decoders for .bin and tlog, parameter dialects and history, cache, fingerprint. Tauri shell, engine sidecar, CLI, platform and ground station setup, import, laptop keys.
> **Gate:** 4.7 logs, the fleet's own included, decode exactly as pymavlink decodes them.

## 1. The gate

**What "exactly" means.** The definition is recorded in SPEC, "Testing", under the Decoder row. The engine's DataFlash decoder must give the same messages, in the same order, with the same values as pymavlink 2.4.50.
- **Order and values:** every field of every message type and instance is compared bit for bit, and NaN is compared by its bits.
- **Scaling:** pymavlink applies only the scaling that format characters imply, such as `L` ×10⁻⁷, and so does the decoder.
- **Corrupt bytes:** pymavlink skips unparseable bytes one at a time, and so does the decoder.

The harness is `fi-engine gate`. Its report holds only counts, type names and where each mismatch occurs, never values or positions, so it can be shared.

Tlogs are decoded by pymavlink itself (bundled, LGPL-3.0, unmodified), so they match by construction. The gate covers DataFlash `.bin` logs.

### 1.1 SITL fixtures (CI, every push)

CI run [39637058665](https://github.com/kyl3n/flight-insights-cl/actions/runs/39637058665) on the head of PR 24 ran `fi-engine gate --corpus tests/fixtures` on Linux and on Windows:

| Firmware | Log | Messages | Result |
| --- | --- | ---: | --- |
| 4.7.0 | copter-quad-hover | 100,701 | match |
| 4.7.0 | copter-quad-rawimu | 148,182 | match |
| 4.7.0 | heli-hover | 90,701 | match |
| 4.7.0 | plane-circuit | 148,859 | match |
| 4.7.0 | quadplane-hover | 146,687 | match |
| 4.7.2 | copter-quad-hover | 100,948 | match |
| 4.7.2 | quadplane-hover | 152,593 | match |
| | **7 logs** | **888,671** | **7 match, 0 mismatches** |

Two more checks guard the decoder on untrusted input:
- **Hypothesis**, in every CI run.
- **Atheris**, on a schedule and on decoder changes.

Atheris has found three real bugs so far. All are fixed, each with a regression test built with `df_builder`:
- two in PR 3, fixed in commit 9e6b116;
- one later, where a shortened `FMT` redefinition could read past the buffer, fixed in commit de839de.

### 1.2 Fleet logs

Two full DataFlash logs from the fleet were run through the gate on 2026-10-22. With the owner's agreement, they were uploaded to a private, temporary development session. They are kept outside the repository, never committed, and go when that session ends. Neither the logs nor any value or position from them appears in the repository, a test or a pull request. The shipped app is unchanged: it never sends location data anywhere.

| | Result |
| --- | --- |
| Logs | 2 (45 MB and 61 MB) |
| Messages compared | 2,733,798 (1,155,088 and 1,578,710) |
| Mismatches | 0 |
| Result | both match, bit for bit |

**Running it on more logs.** More logs, and more varied ones, make the result stronger: other airframes, long flights, and logs damaged by power loss or a full card. Run this on a machine that holds them, using a checkout of `claude/festive-lamport-6t4zhx-package` or later:

```
cd engine
uv sync
uv run fi-engine gate --corpus "$FI_CORPUS_DIR" --report gate-report.json
```

The report it writes holds only counts, type names and content hashes, so it can be shared.

**How a mismatch is fixed:**
1. Find its cause from the report.
2. Reproduce it in a synthetic `df_builder` test, never with fleet bytes.
3. Fix it on the decoder branch.
4. Merge the fix down the stack.
5. Rerun until the report shows zero.

## 2. Benchmarks

### 2.1 Decode speed

| | Measured | Target (SPEC, "Data ingestion and processing") |
| --- | --- | --- |
| 505 MB raw-IMU log, every sample decoded | **8.9 s** (index 6.5 s, decode 2.4 s), 57 MB/s | under 60 s on a 4-core laptop |
| Messages and rows | 14,521,254 of each, 71 types | |
| Peak memory | 2.1 GB | none set |

**Where these numbers come from:**
- **Runner:** the CI job "Decode benchmark" on an ubuntu-latest runner with 4 vCPUs, run [39637058665](https://github.com/kyl3n/flight-insights-cl/actions/runs/39637058665).
- **Log:** `bench/make_large_log.py` builds it by replaying the scrubbed copter-quad-rawimu fixture with its time shifted forward each pass. A real 500 MB log has more varied message types, so treat the speed as indicative.

**Peak memory.** The benchmark decodes every type into memory at once, and that is where the 2.1 GB peak comes from. Import and caching use the streaming path, `iter_batches`, which never holds a whole type at once. Analyzers in Phase 1 should use the streaming path too, or memory becomes the limit on 8 GB laptops before speed does.

### 2.2 Sizes

| Item | Size |
| --- | ---: |
| Frozen engine, Linux | 83.5 MB (100.7 MB before the pyarrow trim) |
| Frozen engine, Windows | 55.7 MB |
| Windows NSIS installer | 59.2 MB |
| Windows MSI | 61.0 MB |
| Fedora RPM | 92.5 MB |
| AppImage | 199.1 MB |
| Knowledge pack (shipped beside the app) | about 1.3 MB |

These come from Package run [39637058802](https://github.com/kyl3n/flight-insights-cl/actions/runs/39637058802), build 0.1.1. Each package was checked before it was kept:
- the NSIS installer was installed silently on Windows, and its bundled engine ran a log import;
- the RPM was installed with `dnf` on a clean Fedora 43;
- the AppImage was unpacked and its engine run.

The AppImage is large because it carries its own WebKitGTK stack. The RPM takes WebKitGTK from the system.

### 2.3 Tests

| Suite | Tests |
| --- | ---: |
| Engine (pytest, Linux and Windows) | 297 |
| Shell (cargo test, Linux and Windows, including a session with the real engine) | 20 |
| UI (vitest) | 32 |
| Fixture tooling and repository checks | 28 |

## 3. What was delivered

Every item on the roadmap line is in place. Each piece is its own pull request, stacked in build order, and all were green in CI on 2026-10-21.

| PR | Piece | Roadmap item |
| --- | --- | --- |
| [#1](https://github.com/kyl3n/flight-insights-cl/pull/1) | Repository skeleton and CI, fixture scrubber, SITL fixtures, spec decisions | CI, test corpus |
| [#2](https://github.com/kyl3n/flight-insights-cl/pull/2) | DataFlash decoder, built only from FMT, FMTU, UNIT and MULT | .bin decoder |
| [#3](https://github.com/kyl3n/flight-insights-cl/pull/3) | Gate harness against pymavlink, fuzzing | gate |
| [#4](https://github.com/kyl3n/flight-insights-cl/pull/4) | Parallel decode, streaming, benchmark | .bin decoder |
| [#5](https://github.com/kyl3n/flight-insights-cl/pull/5) | tlog decoder: parameter reads and writes, message rates | tlog decoder |
| [#6](https://github.com/kyl3n/flight-insights-cl/pull/6) | `.param` and `.params` files, Methodic Configurator files | parameter dialects |
| [#7](https://github.com/kyl3n/flight-insights-cl/pull/7) | Knowledge pack from ArduPilot's own metadata, canonical names, dialect writer | parameter dialects |
| [#8](https://github.com/kyl3n/flight-insights-cl/pull/8) | Encrypted store (SQLCipher, Parquet modular encryption, position key), HLC, migrations | cache |
| [#9](https://github.com/kyl3n/flight-insights-cl/pull/9) | Parameter snapshots, set IDs, change sets, export in the firmware's dialect | parameter history |
| [#10](https://github.com/kyl3n/flight-insights-cl/pull/10) | Log validation, flight splitting, modes, natural flight identity | cache, import |
| [#11](https://github.com/kyl3n/flight-insights-cl/pull/11) | Steady-hover windows | fingerprint |
| [#12](https://github.com/kyl3n/flight-insights-cl/pull/12) | Vehicle fingerprint, keyed board-ID hash | fingerprint |
| [#13](https://github.com/kyl3n/flight-insights-cl/pull/13) | Archetype scorer, rule pack v0 (uncalibrated) | fingerprint |
| [#14](https://github.com/kyl3n/flight-insights-cl/pull/14) | JSON-RPC over stdio, Arrow IPC, CLI, PyInstaller freeze, no-network test | engine sidecar, CLI |
| [#15](https://github.com/kyl3n/flight-insights-cl/pull/15) | Tauri shell: sidecar lifecycle, keychain, laptop identity and keys | Tauri shell, laptop keys |
| [#16](https://github.com/kyl3n/flight-insights-cl/pull/16) | UI shell (Svelte 5): scope switcher, status language, themes | Tauri shell |
| [#17](https://github.com/kyl3n/flight-insights-cl/pull/17) | Platforms and airframes in the engine, build versions | platform setup |
| [#18](https://github.com/kyl3n/flight-insights-cl/pull/18) | Ten-step platform setup wizard | platform setup |
| [#19](https://github.com/kyl3n/flight-insights-cl/pull/19) | Ground station profiles, "detect this computer" | ground station setup |
| [#20](https://github.com/kyl3n/flight-insights-cl/pull/20) | Stations view | ground station setup |
| [#21](https://github.com/kyl3n/flight-insights-cl/pull/21) | Import pipeline: scan, airframe proposal, hardware changes with evidence, hold queue | import |
| [#22](https://github.com/kyl3n/flight-insights-cl/pull/22) | Import dialog | import |
| [#23](https://github.com/kyl3n/flight-insights-cl/pull/23) | Watched save folders, offered for import, never imported unasked | import |
| [#24](https://github.com/kyl3n/flight-insights-cl/pull/24) | Installers: Windows NSIS and MSI, Fedora RPM and AppImage, install checks | packaging |

**Not done in Phase 0, by design or by dependency:**
- **Code signing** waits for certificates.
- **The session monitor** is in Phase 2.
- **Log download over MAVLink** is in Phase 4.
- **Analyzers, the scrubber and AI layer, Drive sync and weather** are Phase 1 and later. Phase 0 laid the hooks they need:
  - position columns are tagged at decode and encrypted under their own key;
  - records carry HLC timestamps and base versions for sync;
  - every flight has its parameter snapshot.

## 4. Spec changes made during Phase 0

Each change was made in the PR that caused it and listed in that PR's description. They are collected here for the Claude Docs master copy.

| PR | Section | Change |
| --- | --- | --- |
| #1 | System architecture, layer table | UI is Svelte 5, no longer "Svelte or React". |
| #1 | Packaging, Linux | RPM and AppImage for recent Fedora and similar distributions, built on the oldest Fedora release still supported. Replaces AppImage and .deb. |
| #1 | AI layer, location scrubber | tlog positions found from `degE7` and degree units. Mission and command coordinates, which carry no units, are found from the `MAV_CMD` parameter definitions. |
| #1 | Storage, encryption at rest | SQLCipher for the database. Parquet modular encryption for the cache, with position columns under their own key. Keys come from the keychain through the shell and are never written to disk. |
| #1 | Testing, log corpus | Scrubbed SITL fixtures are committed with a hash manifest. Fleet logs stay on engineers' machines, where the gate runs. |
| #1 | Testing, Decoder row | The gate defined as exact, bit-for-bit agreement with pymavlink: messages, order and values. Units and SI conversion are a separate, tested layer. |
| #1 | Risks, open questions | pymavlink (LGPL-3.0) ships unmodified and replaceable. Legal review added. |
| #7 | Parameter dialects | Canonical names are the newest release's names. Metadata comes from ArduPilot's own generator. A rename needs proof from both releases' source; the name-changes page is only a cross-check. A cited supplement covers what the generator leaves out. 4.7.1 renamed `PSC_JERK_D`/`PSC_JERK_NE`. |
| #7 | Parameter classes | New "hardware identity" class: device IDs, which mean a build change. |
| #7 | Open questions | Legal review of shipping metadata generated from ArduPilot's GPLv3 source. |
| #9 | Parameter history | The set ID hashes the tuning, calibration and hardware identity classes in canonical names. Restore files can use either dialect and leave out counters and device IDs. |
| #12 | Airframe identity | The board-ID hash is HMAC-SHA-256 with a key from the fleet key, hashed the moment the ID is read. |
| #19 | Ground stations, versions | A version is editable until a flight or file refers to it. Values that change by the minute never create a version. |
| #19 | Ground stations | New: "Recognising the computer". The host name is stored only as a keyed hash; software is detected; a person's own choice stands. |
| #21 | Platforms, import | New: "How a log is matched". Covers the confidence rules, held flights, a new board always recording a build change, and tlog pairing. |
| #23 | Ground stations, setup step 5 | Watched folders offer files for import; nothing is stored until a person confirms. Declined files are not offered again. |
| #24 | Packaging | Unused library parts are trimmed from the sidecar. Version 0.1.<build> on every build. The pack ships beside the app. Every package is installed and checked before it is kept. |

## 5. Open follow-ups

| Item | When |
| --- | --- |
| Run the fleet gate on more, and more varied, fleet logs (section 1.2) | Whenever new logs come in, and before analyzers are calibrated on them |
| Regenerate the copter-quad-rawimu fixture. The current one has logger dropouts | Before Phase 1 analyzers use it |
| Give the quad SITL scenario ESC RPM telemetry, for the propulsion peak and the filter work | Before Phase 1 filter analyzers |
| Derive the board-ID hash key from the fleet key. Until sync exists it is a per-laptop key, so two laptops give different hashes for one board | With Drive sync, Phase 1 |
| Ukrainian UI: choice values and engine notes are still in English; the rail label "Налаштування" breaks mid-word | Phase 1 UI work |
| Move the Fedora build and install jobs to Fedora 44 | When Fedora 43 reaches end of life, 2026-12-09 |
| Code-sign the Windows installers | When certificates arrive |
| Keep analyzer memory bounded by using the streaming decode path (section 2.1) | Phase 1, as a review rule |

## 6. Re-estimating Phase 1

SPEC asks for a re-estimate after Phase 0. Its current plan:

- **Phase 1:** about 8 weeks with three engineers, about 12 with two.
- **Scope:** filters, vibration, power, ESC and motors, bus and CPU, configuration review, recommendations, trends, explanations, encrypted Drive sync and the AI layer.
- **Gate:** zero scrubber leaks, a clean sync soak test, and filter prediction on target.

**What Phase 0 showed.** Writing and testing the code went much faster than the roadmap assumed. The whole phase was built in two days of agent-assisted work, with CI as the check. That speed does not carry over to Phase 1, because Phase 1's critical path is mostly not code:

1. **Labelled logs.** "Filter prediction on target" needs a labelled corpus of real Copter flights, with known-good filter settings to predict against. Labelling has not started. The reference airframe set and the labellers are still an open question in SPEC. This is the longest pole: it needs the fleet's flights and an engineer's judgement on each.
2. **Sync soak test.** It needs elapsed time, several laptops and a real Google Drive, plus Google Cloud project and OAuth consent setup for the Drive API. None of this can be compressed by writing code faster.
3. **Scrubber assurance.** Zero leaks is proven with an adversarial packet corpus and a red-team review of real packets, as in SPEC's Risks table. Both need people outside the author.
4. **Review.** Twenty-four Phase 0 PRs are waiting for human review. If Phase 1 runs at the same pace, review becomes the bottleneck.

**Estimate.** Keep about 8 weeks of calendar time with three engineers (12 with two). Expect the engineering to take roughly the first half, and the rest to go on calibration against labelled logs, the soak test and the scrubber review. If labelling starts now, it is off the critical path by the time the analyzers need it. If labelling starts only when Phase 1 opens, add 2 to 4 weeks.

**Decisions needed before Phase 1 starts:**
- **Reference airframes:** which airframes form the reference set, and who labels their logs (open question in SPEC).
- **Accounts:** a Google Cloud project for the Drive API, and an Anthropic API account under the organization's terms (open question in SPEC).
- **Code signing:** the Windows signing certificates.
