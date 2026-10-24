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
| `copter-heavylift-light` | Copter, coaxial X8 | ArduPilot's Callisto heavy-lift model (32.5 kg, 30-inch props): 15 s hover at 5 m with motor vibration and ESC RPM, raw IMU logged during the hover only. Planted: a throttle-tracking notch although ESC RPM is present, and hover learning off |
| `copter-heavylift-loaded` | Copter, coaxial X8 | The same airframe and motors carrying 8 kg more (hover throttle 0.47 against 0.38), so it pairs with the light flight as one airframe with and without payload |
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
