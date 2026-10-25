# Phase 1 report: Copter filters and health

Status on 2026-10-25. **Phase 1 is built and green in CI** (PRs #27–#88, including the step-by-step procedures of section 2.1). Two of its three gate checks pass. The third passes after three clean nights of the soak on the real drive:

| Gate check | State | What it needs |
| --- | --- | --- |
| Zero scrubber leaks | **passes**, in CI on every change | nothing |
| Sync soak test clean | clean on a local folder drive, and on the real test drive on 2026-10-25 | **three clean nightly runs in a row** (your decision), from 26 October |
| Filter prediction on target | **passes**, closed on 2026-10-25 against the provisional target per class (your decision) | nothing; the first HL2 flight and the first flight with fast notch logging confirm the targets later |

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

**On the real shared drive:** the nightly "Sync soak on the drive" workflow runs the same harness on the test shared drive at 01:41 UTC, with seeds 1, 2 and 3, each run in its own folder. The Google setup was finished on 2026-10-25.
- **The first two runs stopped at their first request** with a bare 404: the drive ID secret held more than the test drive's ID. #87 builds the sign-in helper ready to run. #88 adds Google's own reason to every Drive error, and makes the soak check that the test account belongs to the drive before writing.
- **With the ID corrected, the first full run was clean** (run 40191572447: seed 1, 30 rounds, 3 laptops and one enrolled mid-run, 18 minutes). The result: "sync soak clean: 77 actions, 13 bad objects refused".

**The gate passes after three clean nights in a row** (your decision, 2026-10-25):

| Night | Run | Seeds clean |
| --- | --- | --- |
| 2026-10-26 | | |
| 2026-10-27 | | |
| 2026-10-28 | | |

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

**Closed on 2026-10-25, against the provisional targets** (your decision). Every log checked meets its class's target, including the hardest real case: HL1's 16 per-motor ESC notches. The app also checks its model on every flight with pre-filter data (the `filter.model_error*` metrics in each analysis), so each later flight keeps testing the targets.

Two flights would close the gaps that remain, without holding up Phase 2:
- **a flight from HL2,** since only HL1 has been checked so far;
- **a flight with fast notch logging on** (`LOG_BITMASK` bit 21; for HL1, 2293759), to check the exact replay on real hardware; so far it has only been checked in the simulator.

When they arrive, the targets are confirmed or tightened, with your approval.

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

### 2.1 A step-by-step procedure for every improvement (WP21)

Added on 2026-10-24 at your request: when there is something to improve, the app says exactly how. Each recommendation, action and better option comes with its steps, in order:
1. hardware and firmware work;
2. each parameter file, with the refresh or reboot the firmware needs between them;
3. settings for the test flight only;
4. bench checks;
5. what to fly;
6. when to stop and land;
7. what the next log must show;
8. a revert file and an undo file.

The next flight grades the change and runs the procedure's checks. Every Copter subsystem now has settings checks with procedures, including the Phase 2 ones (failsafes, GNSS, EKF and compass, tuning starting values and AutoTune, navigation). Their log analysis stays in Phase 2.

| Area | PRs |
| --- | --- |
| Filter model: every harmonic notch set the board has | #70 |
| Parameter gates read from the firmware's source and checked in SITL | #71 |
| Procedures, the session card, revert and undo files | #72 |
| The next flight checks each procedure; test-flight settings come out again | #73 |
| UI: procedures, session card, files, print, opportunities, coverage | #74 |
| Filters: FFT settings, resonances, harmonics past the gyro's limit | #75 |
| Logging and the control loop: rate caps, the write buffer, boot-only reboots | #76 |
| Power and ESC settings: every battery monitor, capacity, calibration, EDT | #77 |
| One processor-heavy change per flight; bus procedures | #78 |
| Failsafe settings, skipped arming checks, pack thresholds | #79 |
| Tuning starting values, the AutoTune procedure, navigation settings | #80 |
| Audit false alarms found by the HL1 parameters-only review | #81 |
| Each flight of a log gets the parameters it armed with | #82 |
| Failsafes found off offered at any stage (your choice) | #83 |
| ESC desync: a collapse, not a spool-up | #84 |
| The revert follow-up after a later plan replaced the lasting changes | #85 |
| Procedure rehearsal in SITL | #86 |
| The Google sign-in helper, built ready to run | #87 |
| Drive errors carry Google's reason; the soak checks its drive first | #88 |

**The SITL rehearsal** (`tools/procedures/rehearse.py`) carries out the procedures as a person would:
- it flies a weaker setup, loads the plan's files with the refreshes and reboots the procedure gives, and flies the test flight;
- it checks that every change is confirmed, that the next plan takes the test-flight settings out, and that the revert and undo files restore what they should;
- it only ever talks to a simulator it started on the same computer.

All three rehearsals pass on 4.7.0, 59 of 59 checks:
- a quad with its notch off (your example);
- the heavy-lift X8 whose notch followed throttle;
- failsafes found off.

Building it found the four app bugs fixed in #82, #84 and #85, and the ordering question you decided in #83.

**The HL1 parameters-only review** (with your approval; parameters only, nothing committed) now shows:
- **Next:** the AutoTune procedure, since the rate gains are still the firmware's defaults.
- **Alongside it:** clearing a `LOG_BITMASK` bit the firmware ignores.
- **Then the operations stage:** the arming charge check and the geofence.
- **A question:** the ESC firmware, which decides whether Extended DShot Telemetry can be offered.

## 3. What waits on the team

Nothing blocks the gate: the nightly soak runs on its own. Neither of these holds it up:
1. **An HL2 flight and a flight with fast notch logging,** when they are flown, to confirm the filter targets (section 1.3). Upload them to the session as before. They are never committed or shown.
2. **The Claude API key** as the `ANTHROPIC_API_KEY` GitHub secret. It turns on the nightly live smoke test from #57, which skips itself without the key.

## 4. Open follow-ups

- `MOT_THST_EXPO` fitted from ESC RPM waits for real ESC-RPM logs: the simulator's RPM is linear in the command, so it cannot check the fit (SPEC roadmap, Phase 2).
- Code signing for the installers still waits on certificates.
- Move the Fedora build to Fedora 44 after 2026-12-09.
