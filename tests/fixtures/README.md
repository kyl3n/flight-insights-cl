# Test fixtures

The only place in the repository where flight data may be committed. Every log, tlog and parameter file here has had its positions shifted by `tools/fixtures/scrub_fixture.py` and is listed with its SHA-256 in `SCRUBBED.sha256`. `tools/check_repo_files.py` fails CI for any flight data that is not listed or no longer matches its hash.

## What is here

`sitl/<version>/` holds ArduPilot SITL sessions. Each scenario has five files:

| File | Contents |
| --- | --- |
| `<scenario>.bin` | The onboard DataFlash log, as the firmware wrote it |
| `<scenario>.tlog` | The ground-station tlog, recorded on the MAVLink link at 4 Hz streams |
| `<scenario>.param` | Every parameter, Mission Planner format (`NAME,VALUE`) |
| `<scenario>.params` | The same, QGroundControl format (tab-separated, with component IDs and types) |
| `<scenario>.yaml` | The label: what was flown and what the log should show (`tools/fixtures/label.schema.json`) |

| Scenario | Vehicle | What it covers |
| --- | --- | --- |
| `copter-quad-hover` | Copter, quad + | GUIDED take-off, 20 s hover, LAND; simulated ESC telemetry |
| `copter-quad-rawimu` | Copter, quad + | 6 s hover at 4 m with simulated motor vibration, ESC RPM and an RPM-tracking harmonic notch; pre- and post-filter gyro logged at full rate (`INS_RAW_LOG_OPT` = 9) |
| `copter-quad-fastnotch` | Copter, quad + | 10 s hover at 4 m in gusty wind with a harmonic notch per motor following ESC RPM, moved at loop rate; raw IMU and fast harmonic notch logging (`LOG_BITMASK` bits 19 and 21) during the hover, so the notch's inputs (`FTN`) are logged at every update and the filters replay exactly |
| `copter-quad-tune-default` | Copter, quad + | The tune-check profile on the default quad, shortened to keep the file small: AltHold at 5 m, 6 doublets on roll and 6 on pitch at 40% stick, with fast attitude and PID logging (`LOG_BITMASK` bits 0 and 12) during them, so `RATE` and the rate PIDs are logged every loop. Simulated ESC telemetry is off, and it flies at speed-up 2 so the logger keeps up |
| `copter-quad-tune-low` | Copter, quad + | The same tune-check profile with the roll and pitch rate gains (P, I, D) at 0.4 of their defaults: a soft rate loop whose bandwidth (about 2.5 Hz) and step response the doublets can measure, where the default quad's loop reaches past 20 Hz, above anything a stick doublet excites |
| `copter-quad-tune-ab` | Copter, quad + | An in-flight A/B test on the default quad: 6 roll doublets in AltHold at 5 m, then the roll rate gains (P, I, D) written to 0.4 of their defaults as a ground station writes them (the log records each as `PARM` while armed), 4 s hands-off, and 6 roll doublets again. `RATE` and the rate PIDs are logged every loop during each set of doublets and 1.2 s after it, so the tune check's own logging switch is also in the log. For the in-flight A/B split (`history/ab.py`): roll tracking worse after the change, with the roll rate P ranked first |
| `copter-quad-tune-knob` | Copter, quad + | The same in-flight A/B test with the transmitter's tuning knob: RC7 is the knob (`RC7_OPTION` 219) for roll rate P (`TUNE` 49, `TUNE_MIN` 0.054, `TUNE_MAX` 0.135), held at its top from before arming, 6 roll doublets in AltHold at 5 m, then turned to its bottom, 4 s hands-off, and 6 roll doublets again. No `PARM` records the change; `PTUN` does, about three times a second. `RC_OVERRIDE_TIME` -1 keeps the knob where the scenario puts it. Same logging as `copter-quad-tune-ab`. For the knob in the in-flight A/B split: roll tracking worse after the turn, with the roll rate P from the knob ranked first |
| `copter-quad-autotune-saved` | Copter, quad + | Roll AutoTune (`AUTOTUNE_AXES` 1) from AltHold at 10 m, starting from the soft rate gains of `copter-quad-tune-low`, run to "AutoTune: Success", then landed and disarmed still in AutoTune so the firmware saves the tuned gains (`AUTOTUNE_SAVEDGAINS`). Streamed messages are capped at 25 Hz (`LOG_FILE_RATEMAX`) to keep the file small; AutoTune's own records are not streamed and are all kept |
| `copter-quad-autotune-aborted` | Copter, quad + | Roll AutoTune on the default quad, stopped after 25 s by switching back to AltHold (`AUTOTUNE_OFF`), so the firmware restores the original gains, then LAND. Same logging cap |
| `copter-quad-fs-gcs` | Copter, quad + | GUIDED take-off to 5 m with a ground station's heartbeats on, then none: the GCS failsafe's RTL, about 5 s after `MAV.mgs` stops advancing. Streamed messages capped at 10 Hz (`LOG_FILE_RATEMAX`), as in all four failsafe flights |
| `copter-quad-fs-battery` | Copter, quad + | GUIDED take-off to 10 m, then the simulated pack to 10.5 V and 8.5 V (`SIM_BATT_VOLTAGE`): the low stage's RTL and the critical stage's LAND, each 2.1 s after the voltage went under its threshold (`BATT_LOW_TIMER` 2) |
| `copter-quad-fs-radio` | Copter, quad + | GUIDED take-off to 5 m, then the receiver's failsafe output (`SIM_RC_FAIL` 2: throttle 950 µs, under `FS_THR_VALUE`) with `FS_THR_ENABLE` 4 and SmartRTL unavailable (`SRTL_POINTS` 0): the failsafe falls back to RTL |
| `copter-quad-fs-off` | Copter, quad + | The ground station lost, then the receiver, with both failsafes off (`FS_GCS_ENABLE` 0, `FS_THR_ENABLE` 0): nothing reacts, and the flight ends in LAND by command. SITL keeps the 950 µs throttle to the end |
| `copter-quad-gnss-jam` | Copter, quad + | GUIDED take-off to 15 m and LOITER with the sticks centred, then the simulated receiver jammed for 25 s (`SIM_GPS1_JAM` 1): the fix flickers, satellites fall to 3, the position wanders by hundreds of metres, and the EKF failsafe lands the aircraft. Streamed messages capped at 10 Hz (`LOG_FILE_RATEMAX`), as in all three GNSS flights |
| `copter-quad-gnss-step` | Copter, quad + | The same take-off, then GNSS moved 60 m north at once for 20 s with the signal healthy (`SIM_GPS1_GLTCH_X`), then back: the estimator rejects each jump, then resets onto GNSS |
| `copter-quad-gnss-ramp` | Copter, quad + | The same take-off, then GNSS dragged 80 m north over 60 s with the signal healthy, held 10 s and let go: the estimator follows the drift (test ratios under 1) and rejects only the jump back |
| `copter-quad-nognss-drill` | Copter, quad + | GUIDED take-off to 15 m, LOITER in a 6 m/s wind, then GNSS switched off with the GPS-disable switch (`RC7_OPTION` 65) for 20 s with no other position source: the estimator stops using GNSS about 4 s in and gives up its position about 7 s in, and the EKF failsafe lands while the aircraft drifts downwind. Streamed messages capped at 10 Hz |
| `copter-quad-nognss-flow` | Copter, quad + | GUIDED take-off to 10 m, LOITER in the same wind with simulated optical flow and a rangefinder as EKF source set 2 (`RC8_OPTION` 90): set 2, GNSS off for 30 s, GNSS back, set 1. The estimator keeps a relative position throughout, with no failsafe |
| `copter-quad-wind` | Copter, quad + | GUIDED take-off to 20 m, then 6 s legs at 3, 6 and 9 m/s on eight courses with the nose 0, 30 or 60 degrees off the course, and a hover facing eight ways, in a gusting 5 m/s wind from the west at every height (`SIM_WIND_SPD` 5, `SIM_WIND_DIR` 270, `SIM_WIND_T` 1, `SIM_WIND_TURB` 1). The wind analyzer must find the set wind and fit the frame model's drag. Streamed messages capped at 10 Hz |
| `copter-quad-wind-calm` | Copter, quad + | The same legs and hover in still air (`SIM_WIND_SPD` 0): the analyzer must measure almost no wind, with the same drag as the windy flight |
| `copter-quad-wind-hover` | Copter, quad + | A hover at 20 m facing north, east, south and west, 15 s each, in the same gusting 5 m/s wind from the west: with no change of speed it cannot measure its own drag, and borrows `copter-quad-wind-calm`'s. Leaning about 12 degrees into the wind, it has no steady-hover window |
| `copter-quad-sweep` | Copter, quad + | The speed-sweep card, shortened: a 20 s hover at 15 m, then legs east and back west, nose first, at 2, 4, 6 and 8 m/s in GUIDED, each holding its speed at least 10 s after the turn, in a gusting 1.5 m/s wind from the west along the legs (`SIM_WIND_SPD` 1.5, `SIM_WIND_DIR` 270, `SIM_WIND_T` 1, `SIM_WIND_TURB` 1). The energy analyzer must count four speeds flown both ways. Streamed messages capped at 10 Hz |
| `copter-heavylift-light` | Copter, coaxial X8 | ArduPilot's Callisto heavy-lift model (32.5 kg, 30-inch props): 15 s hover at 5 m with motor vibration and ESC RPM, raw IMU logged during the hover only. Planted: a throttle-tracking notch although ESC RPM is present, and hover learning off |
| `copter-heavylift-loaded` | Copter, coaxial X8 | The same airframe and motors carrying 8 kg more (hover throttle 0.47 against 0.38), so it pairs with the light flight as one airframe with and without payload |
| `copter-heavylift-tune` | Copter, coaxial X8 | The light Callisto flying the tune-check doublets (3 each on roll, pitch and yaw in AltHold at 5 m) with RATE logged every loop during them and ESC RPM from all eight motors, for timing the motors. SITL models no motor dynamics: each motor's RPM follows its command within the simulation step and the log's timing (about 5 ms) |
| `heli-hover` | Traditional helicopter | Rotor run-up, take-off, 20 s hover, LAND |
| `plane-circuit` | Fixed-wing | AUTO take-off, three-waypoint circuit, RTL; ends with a forced disarm in the air |
| `quadplane-hover` | QuadPlane | QLOITER climb and 20 s hover on the VTOL motors, QLAND |

All of them are committed for 4.7.0, the oldest supported firmware. For the newest release, `copter-quad-hover` and `quadplane-hover` cover one Copter and one Plane build. Each label records the exact ArduPilot commit.

SITL always logs its own state (`SIM`, `SIM2`), which real logs do not have. That is the larger part of some of these files.

## How the scrubbing works

Positions are found from what the files say about themselves, not from message names: in DataFlash logs, every field whose FMTU unit is a latitude or longitude; in tlogs, every MAVLink field in `degE7` or degrees named as a latitude or longitude, plus command coordinates whose `MAV_CMD` definition says so. All of them, and parameters named `*_LAT`, `*_LNG` or `*_LON`, move by one random shift per session. The shift is never printed or stored. Values at or within about 1 km of 0,0 (no fix yet) are left alone, so they cannot give the shift away.

Bytes in a DataFlash log that are not a complete message, such as junk between messages or a message cut off at the end of the file, are zeroed in place, because nobody can tell whether they hold part of a position. The scrubber also sets `COMPASS_DEC` to 0, zeroes board unique IDs (in `MSG`/`STATUSTEXT` text and `AUTOPILOT_VERSION`) and blanks the embedded `@SYS/storage.bin`, which copies every parameter and the mission.

It then checks its own output against the input with pymavlink, independently of how it made the changes: the same messages in the same order, every position moved by exactly the shift, everything else unchanged, and none of the original positions (every sample of every position field) anywhere in the bytes, whether as an integer ×10⁷, a float, a double or decimal text. Doubles and text count wherever they appear. A 4-byte integer or float counts only next to a 4-byte encoding of the other coordinate, as positions are stored, because a lone 4-byte match turns up by chance among the millions of 4-byte windows in a large log. If any check fails, it writes nothing.

## Adding fixtures

Run the **SITL fixtures** workflow (Actions tab, `workflow_dispatch`) with an ArduPilot version and the scenarios. Download its artifact, which is this folder with the new files and manifest entries added, and review it. Then commit it. Locally, with ArduPilot built for SITL (`./waf configure --board sitl && ./waf copter heli plane`):

```sh
tools/fixtures/sitl/make_fixtures.sh ~/ardupilot 4.7.0 tests/fixtures copter-quad-hover
```

Never edit a fixture by hand; regenerate it.

## Logs from real aircraft

Real fleet logs stay out of the repository. The Phase 0 gate runs against them by hand from a local folder (`FI_CORPUS_DIR`). Before a real log could ever become a fixture, an engineer has to review more than the scrubber covers:
- **Absolute altitudes are not changed.** Together with terrain, they can narrow down a region.
- **Mission, fence and rally files** saved beside the log.
- **Free text** such as operator notes.
