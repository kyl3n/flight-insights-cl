# CMU DJI Matrice 100: steady windows (public data)

`windows.csv` holds 2 s window averages from a public data set flown by Carnegie Mellon University. The power model's check (`engine/tests/test_public_m100.py`) fits it to see whether the model's shape holds on a real airframe whose airspeed was measured with an anemometer.

## Credit and licence

- **Data:** Rodrigues, T. A., Patrikar, J., Choudhry, A., Feldgoise, J., Arcot, V., Gahlaut, A., Lau, S., Moon, B., Wagner, B., Matthews, H. S., Scherer, S. and Samaras, C., "Data Collected with Package Delivery Quadcopter Drone", Carnegie Mellon University KiltHub, 2021, [doi:10.1184/R1/12683453.v1](https://doi.org/10.1184/R1/12683453.v1).
- **Paper:** Rodrigues, T. A. et al., "In-flight positional and energy use data set of a DJI Matrice 100 quadcopter for small package delivery", *Scientific Data* 8, 155 (2021), [doi:10.1038/s41597-021-00930-x](https://doi.org/10.1038/s41597-021-00930-x).
- **Licence:** [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), as the repository states for the data set (checked 2026-10-29).
- **Changes made:** the published per-flight samples were reduced to steady window averages, as described below. Positions, heights, headings, times and dates were removed. The airspeed was worked out again from the anemometer and the ground velocity. The take-off mass was added from the paper.

## The source files

These were downloaded on 2026-10-29. They aren't committed, because they hold the aircraft's positions.

| File | SHA-256 |
| --- | --- |
| `parameters.csv` | `568dffaed11a4d335ef4f8a932b342c836907af2f4aa54da9b4a0e2ffef2108a` |
| `flights.zip` | `98f65a1ab54a8b618c169fdf49311a53ff1fefed86fdc3f440d6a0166ce49fb1` |

To make the file again:

```
uv run tools/fixtures/public/cmu_m100.py parameters.csv flights.zip tests/fixtures/public/cmu-m100/windows.csv
```

## The airframe

- **Aircraft:** a DJI Matrice 100 quadcopter with four 13-inch propellers, one TB48D pack, an FT205 anemometer on a mast, and an onboard computer.
- **Take-off masses:** 3.68 kg empty, plus payloads of 250 g, 500 g and, on one flight, 750 g (from the paper, "Airframe").
- **Flights:** the 200 flights that left the ground. Routes R1–R7 are cruises at a commanded ground speed of 4–12 m/s; route H is hovers, which stayed under 5 m above take-off and so give no windows. The ground tests (A1–A3) are left out. 196 flights give windows.

## How the windows are made

- **Ground velocity:** the logged body-frame velocity rotated by the attitude quaternion into east, north and up.
- **Airspeed:**
  - The anemometer gives the relative air's speed and the bearing it comes from. The paper's Usage Notes say its magnitude reads high, more so at low speed.
  - So for each flight, the air vector `r = c + k * v_ground` is fitted over the samples where the aircraft moves. The flight's wind is `-c / k`, and airspeed is `|v_ground - wind|`.
  - Across the flights, the median `k` is 1.018. The one flight that hardly moves takes that median.
  - `anemometer` keeps the raw reading's mean for comparison.
- **Phases:** the engine's own rules (`knowledge/rules/energy.json`, "phases"): 2 s tiles of at least 10 samples, more than 5 m above the take-off point. Each tile is a climb or descent, a hover or level flight; manoeuvres are dropped.
- **Steadiness:** a window whose power varies more than the power model's points allow (`knowledge/rules/performance.json`, "points", `max_power_cv`) is left out.
- **Power:** the pack's voltage times its current, as measured after the battery.
- **Air density:** one value for every row, from the standard atmosphere at the median take-off elevation, rounded to 0.01 kg/m³. The temperature was not logged.

## Columns

| Column | Meaning |
| --- | --- |
| `flight` | `m100-<flight number>` |
| `payload_g`, `mass_kg` | The payload and the take-off mass |
| `speed_set_ms` | The commanded cruise ground speed |
| `wind_from` | `own fit`, or `median k` for the one flight that hardly moves |
| `density` | Air density, kg/m³ |
| `phase` | `hover`, `level`, `climb` or `descent` |
| `seconds` | The window's length |
| `airspeed` | Airspeed through the wind, m/s |
| `anemometer` | The anemometer's raw mean reading, m/s |
| `ground` | Horizontal ground speed, m/s |
| `climb` | Vertical speed, up positive, m/s |
| `power` | The pack's power, W |
| `volts` | The pack's voltage, V |
