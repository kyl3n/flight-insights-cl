# Screen mock-ups

These are the source files of the Flight Insights layout canvas. The live, viewable version is the design canvas linked from the spec's "Layout" section. Opened directly, these files won't render properly, because they rely on the canvas runtime. Read them as a layout reference: they give the structure, content, spacing, colours and interactions for each screen.

| File | Screen |
| --- | --- |
| `Main.dc.html` | Fleet overview |
| `FlightReport.dc.html` | Flight report |
| `Workbench.dc.html` | Filter workbench |
| `Tune.dc.html` | Tune: tracking error, step and frequency responses, against an earlier flight |
| `ConfigReview.dc.html` | Configuration review |
| `Trends.dc.html` | Trends and what changed |
| `Plan.dc.html` | Tuning plan |
| `MetricHover.dc.html` | Metric on hover or focus |
| `MetricExplain.dc.html` | Metric on click: full explanation |
| `PlatformSetup.dc.html` | New platform setup |
| `ImportDialog.dc.html` | Import: airframe and hardware changes |
| `Propulsion.dc.html` | Flight: thrust curve from ESC RPM, payload estimate and bands, motor-out margin by payload, energy for this flight |
| `History.dc.html` | Parameter history: change sets with their measured effect, in-flight A/B splits (a ground station's write or a tuning-knob turn), and the change behind a shift |
| `Incidents.dc.html` | Flight timeline: every source on one axis, first fault, failsafes checked, lead time, near misses |
| `Links.dc.html` | Flight links: loss, outages, latency and joystick input over the radio, the failsafe settings for the link, signal against distance, antenna pattern. Signal against distance and the radios' own statistics need the radios' own logs, a later version; until then the screen shows them only for radios that report through ArduPilot (SiK, RFD) |
| `Navigation.dc.html` | Flight navigation and GNSS: events and their class, how the aircraft coped, readiness without GNSS |
| `RecoveryCard.dc.html` | Printable recovery card per airframe; its link section worded as the engine builds it |
| `Performance.dc.html` | Airframe range and speed: endurance, range and radius by payload, pack, air, wind and trip; power against airspeed; the speed to fly into a headwind; batteries; what-ifs; a planned build against the flying one; the speed build-up; the data behind the model; recommendations for the plan |
| `RangeCard.dc.html` | Printable range card per airframe: time, range, radius and speeds for each payload, with the reserve rule, the parameters it was worked out with and its validity |
| `Weather.dc.html` | Flight weather: the record, measured against model wind, wind envelope |
| `SettingsWeather.dc.html` | Settings: weather key and download status |

All data in the mock-ups is illustrative.
