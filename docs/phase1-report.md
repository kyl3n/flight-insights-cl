# Phase 1 report: Copter filters and health

Status on 2026-10-25. **Phase 1 is built and green in CI** (PRs #27–#69). Of its three gate checks, one passes and two wait on inputs from the team:

| Gate check | State | What it needs |
| --- | --- | --- |
| Zero scrubber leaks | **passes**, in CI on every change | nothing |
| Sync soak test clean | clean on a local folder drive; **the real-drive run waits** | the Google setup in `docs/google-setup.md`, then several clean nightly runs |
| Filter prediction on target | **passes on the 6 logs checked so far**, against a provisional target per class | the HL1/HL2 raw-IMU logs, to confirm the target on the reference set |

The roadmap line for Phase 1 (`docs/SPEC.md`, "Roadmap and milestones"):

> Filters, vibration, power, ESC and motors, bus and CPU, configuration review. Recommendations, trends, explanations, encrypted Drive sync, AI layer.
> **Gate:** zero scrubber leaks, sync soak test clean, filter prediction on target.

## 1. The gate

### 1.1 Zero scrubber leaks

Nothing sent to the Claude API may reveal where the aircraft flew (CLAUDE.md, "Location and privacy"). The check runs in two independent layers:
- **the engine's scrubber,** deny by default, against a strict packet schema, with coordinate detectors and the flight's own positions as a tripwire;
- **the shell's own check,** written separately in Rust with its own schema and detectors, run just before anything is sent.

A start-up self-test of both blocks AI calls until it passes.

**The CI gate** ("Scrubber gate (no planted coordinate leaves)") runs on every change: 98 tests, zero tolerance.
- **The planted corpus:** one known coordinate is planted in every position-tagged or blocked field of every 4.7.x message type, in tlog position fields, mission and command coordinates, RTCM, location parameters and free text in several notations. Each source is imported, analysed and turned into a packet. No planted value may appear in it in any encoding.
- **Shared packets** (`tests/scrubber/packets/`): 5 clean and 26 leaky packets that both implementations must judge the same way. Every leaky one is refused by both.
- **Tool results and chat questions** are packets too, and pass both checks the same way.

**Result: zero leaks**, on main at the time of writing (run locally and in CI).

### 1.2 Sync soak test

The harness (`shell/src-tauri/tests/soak.rs`) runs simulated laptops, each a real engine with its own encrypted store, sharing one drive. Each run is a seeded, random mix of:
- imports, including the same log on two laptops;
- the same field edited at the same time;
- recommendation decisions and AI results;
- crashes and offline rounds;
- flipped, cut and renamed objects;
- a laptop enrolled mid-run, and another removed with a key rotation.

After every round, nothing on the drive was deleted or changed. Once everything has synced:
- every laptop still in the fleet holds identical synced records;
- every conflict kept both values;
- no good object was quarantined, and every bad one was;
- the removed laptop read nothing written after its removal.

**On a local folder drive:** every PR runs a short soak (seed 1, 10 rounds, 3 laptops). A longer run of 8 seeds × 30 rounds × 4 laptops found a fourth divergence (seed 1, about 2 runs in 3): twin records merged differently depending on the order the laptops met, and a recommendation's airframe did not follow its flight. Both are fixed in #68, with a regression test over five meeting orders of three laptops. After the fix all 8 seeds are clean, and seed 1 was clean on 4 runs in a row.

The soak found three real divergences while it was built (#62), all fixed, and a fourth since (above):
- a float lost its last bit in JSON;
- twin records merged without their children;
- a decided recommendation was planned again.

**On the real shared drive:** the nightly "Sync soak on the drive" workflow runs the same harness on a test shared drive, each run in its own folder. It is skipped until the Google setup in `docs/google-setup.md` is done. **The gate passes after several clean nights in a row.**

### 1.3 Filter prediction

The app predicts the filtered gyro spectrum from the pre-filter one, using its own copy of ArduPilot's filters (low-pass, notch, harmonic notch with every tracking mode). `fi-engine filter-check` compares the prediction with the filtered data the autopilot itself logged (`INS_RAW_LOG_OPT` = 9), in steady hover, as an error in dB. Its report names logs only by a content hash and holds only errors, so it can be shared.

**The target depends on what the notches followed.** A dynamic notch moves with its inputs (throttle, ESC RPM or FFT). When the log holds those inputs at every update, the model replays them exactly. When it holds them less often than the firmware used them, the model rebuilds them from the rows it has, and the gaps cost accuracy that has nothing to do with the filters. Each class is its own metric (#69):

| Metric | When | Target (provisional) |
| --- | --- | --- |
| `filter.model_error` | inputs known exactly: no notch, a fixed notch, `REF` 0, or inputs logged at every update | < 0.1 dB |
| `filter.model_error_rebuilt` | inputs rebuilt from rows logged less often than the firmware used them | < 1.5 dB |
| `filter.model_error_control` | 0–40 Hz, the band the rate controller acts on, every flight | < 0.1 dB |

The targets live in `knowledge/metrics/filter.json`, and `filter-check` holds each log to its class.

**Simulator logs** (`tests/fixtures`), the four with pre-filter data and a hover, run on 2026-10-25:

| Log | Notch inputs | Error | Control band | Result |
| --- | --- | ---: | ---: | --- |
| copter-heavylift-light | rebuilt (throttle) | 0.036 dB | 0.000 dB | pass |
| copter-heavylift-loaded | rebuilt (throttle) | 0.009 dB | 0.000 dB | pass |
| copter-quad-fastnotch | exact (ESC RPM, logged at every update) | 0.021 dB | 0.008 dB | pass |
| copter-quad-rawimu | rebuilt (ESC RPM) | 0.042 dB | 0.001 dB | pass |

The other fixtures have no pre-filter gyro data or no hover, and are reported as such. On the fast-notch fixture, rebuilding the inputs from its 100 Hz ESC rows instead gives 0.284 dB: the same flight, more than ten times worse, only because of how often the inputs were logged.

**The two fleet logs from HL1** (aggregate only, with your approval):
- the first, with the notch off: 0.007 dB (exact class), pass;
- the second: 1.142 dB (rebuilt class), 0.001 dB in the control band, pass.

**The 1.142 dB is explained.** With bidirectional DShot, the firmware moves HL1's 16 per-motor notches 400 times a second, but the log keeps ESC RPM at 100 Hz and the notch inputs at 10 Hz. However those gaps are filled, the rebuilt notch centres stay about 0.8 Hz from the firmware's own. Placement and notch shape match the 4.7.0 source exactly. A flight with fast harmonic notch logging on (`LOG_BITMASK` bit 21) records the inputs at every update, and the app then replays them exactly. The new finding `logging.notch_inputs_not_logged` proposes it for filter-tuning flights.

**Still to do:** check the target on the HL1/HL2 raw-IMU logs, ideally with fast notch logging on, and confirm or tighten the provisional numbers with your approval.

## 2. Delivered against the roadmap line

| Area | PRs |
| --- | --- |
| Spec: Better options, reference airframes HL1 and HL2, log labels | #27 |
| Validation: a jittered IMU clock or raw logging switched off is not sample loss | #28 |
| Fixtures: vibrating raw-IMU hover; heavy-lift X8 light and loaded | #29 |
| Analysis framework, metric registry, vibration | #30 |
| Logging adequacy and system health | #31 |
| Spectral core and ESC RPM | #32 |
| Filter simulation, the model check, `filter-check` | #33 |
| Filter analyzer: tracking source, setup flags, candidate search | #34, #35 |
| Power; motors and ESCs; buses and interfaces | #36, #37, #38 |
| Better options: catalog and engine | #39 |
| Recommendations: engine, closing the loop, parameter audit | #40, #41, #42 |
| Trends and ideal ranges | #43 |
| UI: flight report, configuration review and plan, filter workbench, fleet and trends, propulsion and system | #44–#48 |
| Checking a parameter file before it is flown | #49 |
| Location scrubber (three parts) | #50–#52 |
| AI layer: exchange, Claude client, summary and plan, Ask, live smoke test | #53–#57 |
| Encrypted sync: engine model, object format and keyring, board-ID key, loop, soak | #58–#62 |
| Google Drive: client and sign-in, sync in the app, service keys and the nightly soak, admin guide | #63–#66 |
| Sync: twins merge the same whatever order the laptops meet in | #68 |
| Filter model: follows logged notch inputs; a target per class; fast notch logging finding | #69 |

Each PR description lists its spec changes for the Claude Docs master.

## 3. What waits on the team

1. **The HL1/HL2 raw-IMU logs**, uploaded to this session as before. They are never committed or shown. The provisional filter targets are checked on them, with your approval. Flights with `LOG_BITMASK` bit 21 on (for HL1, 2293759) give exact notch inputs.
2. **The rest of the Google setup** (`docs/google-setup.md`). `FI_GOOGLE_CLIENT_ID` and `FI_GOOGLE_CLIENT_SECRET` are in GitHub. Still needed: the test account's sign-in (step 6), which gives `FI_GOOGLE_TEST_REFRESH_TOKEN` and `FI_GOOGLE_TEST_DRIVE_ID`. Then the nightly real-drive soak runs on its own.
3. **The Claude API key**, as a GitHub secret, if not already added. It turns on the nightly live smoke test from #57.

## 4. Open follow-ups

- **In progress before this report is final:** a step-by-step procedure for every improvement, across every Copter setting (WP21, approved 2026-10-24). It adds abort criteria, temporary test-flight settings with revert and undo files, settings checks for every subsystem, and a SITL rehearsal of each procedure.
- Code signing for the installers still waits on certificates.
- Move the Fedora build to Fedora 44 after 2026-12-09.
