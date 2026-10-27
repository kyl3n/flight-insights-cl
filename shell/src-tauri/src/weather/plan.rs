//! What the weather downloader asks for, and when (docs/SPEC.md, "Weather and wind", "How
//! the location stays private"). Pure: the clock, the tiles and what is already held go
//! in; the requests due come out, with no key and no network.
//!
//! Every request goes out at a slot instant: every 6 hours from 00:00 UTC, plus the
//! schedule's offset minute. What a request asks for depends only on that slot, the
//! tile and what is already held, never on when anyone flew:
//!
//! - **Forecast:** each slot asks for the 6 hours that ended 6 hours before it, from
//!   ECMWF IFS HRES; windows missed while the laptop was off are asked for at a later
//!   slot, for up to 5 days, after which reanalysis covers them.
//! - **Reanalysis:** ERA5 over a fixed window, the last 24 months, up to 6 days ago:
//!   whole calendar months once a month is complete, single days for the rest. A new
//!   tile takes a few slots to fill: 2 months a slot.
//! - **Space weather:** NOAA's planetary K-index, once a slot, with no parameters.
//! - **A new tile** waits a random delay of 1 to 72 hours before any request names it.

use std::collections::HashSet;

use crate::sync::sodium;

pub const HOUR_MS: i64 = 3_600_000;
pub const DAY_MS: i64 = 24 * HOUR_MS;
pub const SLOT_MS: i64 = 6 * HOUR_MS;
/// A slot asks for the six hours that ended this long before it, so the model run that
/// covers them is out.
const FORECAST_LAG_MS: i64 = 12 * HOUR_MS;
/// Forecast windows older than this are left to reanalysis.
const FORECAST_CATCH_UP_MS: i64 = 5 * DAY_MS;
/// ERA5 is published about 5 days after the fact: a day is asked for 6 days on.
const REANALYSIS_LAG_DAYS: i64 = 6;
const BACKFILL_MONTHS: i64 = 24;
const MONTHS_PER_SLOT: usize = 2;
const DELAY_MIN_MS: i64 = HOUR_MS;
const DELAY_SPAN_MS: i64 = 71 * HOUR_MS;

pub const FORECAST_MODEL: &str = "ecmwf_ifs";
pub const REANALYSIS_MODEL: &str = "era5";
pub const FORECAST_HOURLY: &[&str] = &[
    "temperature_2m",
    "relative_humidity_2m",
    "dew_point_2m",
    "surface_pressure",
    "pressure_msl",
    "precipitation",
    "cloud_cover",
    "cloud_cover_low",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
    "wind_speed_100m",
    "wind_direction_100m",
    "wind_speed_200m",
    "wind_direction_200m",
    "boundary_layer_height",
];
pub const REANALYSIS_HOURLY: &[&str] = &[
    "temperature_2m",
    "relative_humidity_2m",
    "dew_point_2m",
    "surface_pressure",
    "pressure_msl",
    "precipitation",
    "cloud_cover",
    "cloud_cover_low",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
    "wind_speed_100m",
    "wind_direction_100m",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Source {
    Forecast,
    Reanalysis,
    Kp,
}

impl Source {
    pub fn name(self) -> &'static str {
        match self {
            Source::Forecast => "forecast",
            Source::Reanalysis => "reanalysis",
            Source::Kp => "kp",
        }
    }
}

/// When requests go out: the minute past each 6-hour slot, and the seed each tile's
/// delay is drawn from. A fleet shares one, so its laptops agree.
#[derive(Debug, Clone)]
pub struct Schedule {
    pub offset_min: u32,
    pub seed: [u8; 32],
}

impl Schedule {
    /// When the slot starting at `slot` sends.
    pub fn instant(&self, slot: i64) -> i64 {
        slot + i64::from(self.offset_min % 360) * 60_000
    }

    /// The slot whose instant is the latest at or before `now`.
    pub fn slot_at(&self, now: i64) -> i64 {
        (now - self.instant(0)).div_euclid(SLOT_MS) * SLOT_MS
    }

    /// The first slot instant after `now`.
    pub fn next_instant(&self, now: i64) -> i64 {
        self.instant(self.slot_at(now) + SLOT_MS)
    }

    /// The random delay before a new tile is first asked for, the same on every start.
    pub fn delay(&self, tile: &str) -> i64 {
        let mut input = self.seed.to_vec();
        input.extend_from_slice(tile.as_bytes());
        let h = sodium::sha256(&input);
        let n = u64::from_le_bytes(h[..8].try_into().expect("8 bytes"));
        DELAY_MIN_MS + (n % DELAY_SPAN_MS as u64) as i64
    }
}

#[derive(Debug, Clone)]
pub struct Tile {
    pub name: String,
    pub added_ms: i64,
}

/// What is already kept, by source, tile and period.
pub type Held = HashSet<(Source, String, String)>;

/// One request: what it is kept as, and its query (no key: added when sent).
#[derive(Debug, Clone, PartialEq)]
pub struct Request {
    pub source: Source,
    pub tile: String,
    pub period: String,
    pub query: Vec<(String, String)>,
}

/// The grids each source is asked on, from the engine: a tile's size and each step.
#[derive(Debug, Clone)]
pub struct Grids {
    pub tile_deg: i64,
    pub forecast_deg: f64,
    pub reanalysis_deg: f64,
}

/// Everything due at the slot starting at `slot`.
pub fn due(
    slot: i64,
    schedule: &Schedule,
    tiles: &[Tile],
    held: &Held,
    grids: &Grids,
) -> Vec<Request> {
    let mut out = Vec::new();
    if !held.contains(&(Source::Kp, String::new(), hour_label(slot))) {
        out.push(Request {
            source: Source::Kp,
            tile: String::new(),
            period: hour_label(slot),
            query: Vec::new(),
        });
    }
    let at = schedule.instant(slot);
    let mut tiles: Vec<&Tile> = tiles.iter().collect();
    tiles.sort_by(|a, b| a.name.cmp(&b.name));
    for tile in tiles {
        if at < tile.added_ms + schedule.delay(&tile.name) {
            continue; // newly listed: not yet
        }
        let Some(corner) = corner(&tile.name) else {
            continue;
        };
        let has = |source: Source, period: &str| {
            held.contains(&(source, tile.name.clone(), period.to_owned()))
        };
        let forecast = points(corner, grids.tile_deg, grids.forecast_deg);
        let mut window = slot - FORECAST_CATCH_UP_MS;
        while window <= slot {
            let start = window - FORECAST_LAG_MS;
            let period = hour_label(start);
            if !has(Source::Forecast, &period) {
                let mut query = forecast.clone();
                query.extend([
                    ("hourly".to_owned(), FORECAST_HOURLY.join(",")),
                    ("models".to_owned(), FORECAST_MODEL.to_owned()),
                    ("start_hour".to_owned(), format!("{}:00", hour_label(start))),
                    (
                        "end_hour".to_owned(),
                        format!("{}:00", hour_label(start + SLOT_MS - HOUR_MS)),
                    ),
                ]);
                query.extend(common());
                out.push(Request {
                    source: Source::Forecast,
                    tile: tile.name.clone(),
                    period,
                    query,
                });
            }
            window += SLOT_MS;
        }
        let reanalysis = points(corner, grids.tile_deg, grids.reanalysis_deg);
        let mut months = 0;
        for (period, first, last) in reanalysis_chunks(slot) {
            if has(Source::Reanalysis, &period) {
                continue;
            }
            let whole_month = period.len() == 7;
            if whole_month {
                if months == MONTHS_PER_SLOT {
                    continue;
                }
                months += 1;
            }
            let mut query = reanalysis.clone();
            query.extend([
                ("hourly".to_owned(), REANALYSIS_HOURLY.join(",")),
                ("models".to_owned(), REANALYSIS_MODEL.to_owned()),
                ("start_date".to_owned(), day_label(first)),
                ("end_date".to_owned(), day_label(last)),
            ]);
            query.extend(common());
            out.push(Request {
                source: Source::Reanalysis,
                tile: tile.name.clone(),
                period,
                query,
            });
        }
    }
    out
}

fn common() -> [(String, String); 3] {
    [
        ("cell_selection".to_owned(), "nearest".to_owned()),
        ("timezone".to_owned(), "GMT".to_owned()),
        ("wind_speed_unit".to_owned(), "ms".to_owned()),
    ]
}

/// The reanalysis a slot covers, newest first, as (period, first day, last day) in days
/// since 1970: whole months that are complete 6 days ago, single days for the rest,
/// back to the start of the month 24 months before the slot's.
fn reanalysis_chunks(slot: i64) -> Vec<(String, i64, i64)> {
    let today = slot.div_euclid(DAY_MS);
    let last_day = today - REANALYSIS_LAG_DAYS;
    let (y, m, _) = civil(today);
    let mut out = Vec::new();
    for back in 0..=BACKFILL_MONTHS {
        let index = y * 12 + (m - 1) - back;
        let (my, mm) = (index.div_euclid(12), index.rem_euclid(12) + 1);
        let first = days(my, mm, 1);
        let end = days(my + (mm == 12) as i64, mm % 12 + 1, 1) - 1;
        if first > last_day {
            continue;
        }
        if end <= last_day {
            out.push((format!("{my:04}-{mm:02}"), first, end));
        } else {
            for d in (first..=last_day).rev() {
                out.push((day_label(d), d, d));
            }
        }
    }
    out
}

/// A tile's south-west corner in whole degrees, from its name ("N50E030").
pub fn corner(tile: &str) -> Option<(i64, i64)> {
    let b = tile.as_bytes();
    if b.len() != 7 || !matches!(b[0], b'N' | b'S') || !matches!(b[3], b'E' | b'W') {
        return None;
    }
    let digits = |s: &[u8]| -> Option<i64> {
        s.iter()
            .all(u8::is_ascii_digit)
            .then(|| s.iter().fold(0, |n, d| n * 10 + i64::from(d - b'0')))
    };
    let lat = digits(&b[1..3])? * if b[0] == b'N' { 1 } else { -1 };
    let lon = digits(&b[4..7])? * if b[3] == b'E' { 1 } else { -1 };
    ((-90..90).contains(&lat) && (-180..180).contains(&lon)).then_some((lat, lon))
}

/// A tile's points as the latitude and longitude lists the API takes: its south and
/// west edges and everything inside, in hundredths of a degree, so every number is
/// exact.
fn points((lat, lon): (i64, i64), tile_deg: i64, step_deg: f64) -> Vec<(String, String)> {
    let step = (step_deg * 100.0).round() as i64;
    let n = tile_deg * 100 / step;
    let (mut lats, mut lons) = (Vec::new(), Vec::new());
    for i in 0..n {
        for j in 0..n {
            lats.push(hundredths(lat * 100 + i * step));
            lons.push(hundredths(lon * 100 + j * step));
        }
    }
    vec![
        ("latitude".to_owned(), lats.join(",")),
        ("longitude".to_owned(), lons.join(",")),
    ]
}

pub(crate) fn hundredths(v: i64) -> String {
    let sign = if v < 0 { "-" } else { "" };
    let (whole, part) = (v.abs() / 100, v.abs() % 100);
    match part {
        0 => format!("{sign}{whole}"),
        p if p % 10 == 0 => format!("{sign}{whole}.{}", p / 10),
        p => format!("{sign}{whole}.{p:02}"),
    }
}

/// Days since 1970-01-01 of a civil date (H. Hinnant's algorithm).
pub fn days(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// The civil date of a day since 1970-01-01.
pub fn civil(z: i64) -> (i64, i64, i64) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (yoe + era * 400 + i64::from(m <= 2), m, d)
}

pub fn day_label(day: i64) -> String {
    let (y, m, d) = civil(day);
    format!("{y:04}-{m:02}-{d:02}")
}

/// An hour as a period: "2026-10-26T06".
pub fn hour_label(ms: i64) -> String {
    let day = ms.div_euclid(DAY_MS);
    format!("{}T{:02}", day_label(day), ms.rem_euclid(DAY_MS) / HOUR_MS)
}

#[cfg(test)]
mod tests {
    use super::*;

    pub const GRIDS: Grids = Grids {
        tile_deg: 2,
        forecast_deg: 0.1,
        reanalysis_deg: 0.25,
    };

    fn at(y: i64, m: i64, d: i64, h: i64) -> i64 {
        days(y, m, d) * DAY_MS + h * HOUR_MS
    }

    fn schedule() -> Schedule {
        Schedule {
            offset_min: 23,
            seed: [7; 32],
        }
    }

    /// What the live weather check sends (engine.rs, live_weather_check): for a tile listed
    /// long ago, at any slot of the year, a forecast window, an ERA5 period and the K-index
    /// are due, with the forecast's points on the 0.1-degree grid.
    #[test]
    fn a_long_listed_tile_has_one_of_each_source_due() {
        let tile = Tile {
            name: "N16W014".into(),
            added_ms: 0,
        };
        for day in (0..365).step_by(7) {
            let slot = schedule().slot_at(at(2026, 1, 1, 0) + day * DAY_MS);
            let due = due(
                slot,
                &schedule(),
                std::slice::from_ref(&tile),
                &Held::new(),
                &GRIDS,
            );
            for source in [Source::Forecast, Source::Reanalysis, Source::Kp] {
                assert!(
                    due.iter().any(|r| r.source == source),
                    "{} on day {day}",
                    source.name()
                );
            }
            let forecast = due.iter().find(|r| r.source == Source::Forecast).unwrap();
            let lats = forecast
                .query
                .iter()
                .find(|(k, _)| k == "latitude")
                .unwrap();
            assert_eq!(lats.1.split(',').count(), 400);
        }
    }

    #[test]
    fn dates_round_trip() {
        assert_eq!(days(1970, 1, 1), 0);
        assert_eq!(days(2000, 3, 1), 11_017);
        for z in -800_000..800_000 {
            let (y, m, d) = civil(z);
            assert_eq!(days(y, m, d), z);
        }
        assert_eq!(hour_label(at(2026, 10, 26, 6)), "2026-10-26T06");
        assert_eq!(day_label(days(2024, 2, 29)), "2024-02-29");
    }

    #[test]
    fn slots_fall_every_six_hours_past_the_offset_minute() {
        let s = schedule();
        let six = at(2026, 10, 26, 6);
        assert_eq!(s.instant(six), six + 23 * 60_000);
        assert_eq!(s.slot_at(six + 23 * 60_000), six);
        assert_eq!(s.slot_at(six + 22 * 60_000), six - SLOT_MS);
        assert_eq!(s.next_instant(six), six + 23 * 60_000);
        assert_eq!(
            s.next_instant(six + 23 * 60_000),
            six + SLOT_MS + 23 * 60_000
        );
        // the delay is fixed per tile and seed, and within 1 to 72 hours
        let d = s.delay("N50E030");
        assert_eq!(d, s.delay("N50E030"));
        assert!((HOUR_MS..72 * HOUR_MS).contains(&d));
        assert_ne!(d, s.delay("N52E030"));
    }

    #[test]
    fn a_tiles_points_match_the_engines_grid() {
        // engine/src/flight_insights/weather/tiles.py grid(): south and west edges and
        // inside, south to north then west to east
        let p = points((50, 30), 2, 0.1);
        let lats: Vec<&str> = p[0].1.split(',').collect();
        let lons: Vec<&str> = p[1].1.split(',').collect();
        assert_eq!((lats.len(), lons.len()), (400, 400));
        assert_eq!((lats[0], lons[0]), ("50", "30"));
        assert_eq!((lats[1], lons[1]), ("50", "30.1"));
        assert_eq!((lats[399], lons[399]), ("51.9", "31.9"));
        let p = points((-34, -60), 2, 0.25);
        let lats: Vec<&str> = p[0].1.split(',').collect();
        let lons: Vec<&str> = p[1].1.split(',').collect();
        assert_eq!(lats.len(), 64);
        assert_eq!((lats[0], lons[0]), ("-34", "-60"));
        assert_eq!((lats[63], lons[63]), ("-32.25", "-58.25"));
        assert_eq!(corner("S90W180"), Some((-90, -180)));
        assert_eq!(corner("N90E000"), None);
        assert_eq!(corner("N50E30"), None);
    }

    #[test]
    fn what_a_slot_asks_for() {
        let s = schedule();
        let slot = at(2026, 10, 26, 12);
        let tile = Tile {
            name: "N50E030".into(),
            added_ms: 0,
        };
        let held = Held::new();
        let all = due(slot, &s, std::slice::from_ref(&tile), &held, &GRIDS);
        assert_eq!(all[0].source, Source::Kp);
        assert!(all[0].query.is_empty());
        let forecast: Vec<&Request> = all
            .iter()
            .filter(|r| r.source == Source::Forecast)
            .collect();
        // five days of windows, the newest the six hours from 00:00 that ended at 06:00
        assert_eq!(forecast.len(), 21);
        let newest = forecast.last().unwrap();
        assert_eq!(newest.period, "2026-10-26T00");
        let q = |r: &Request, k: &str| r.query.iter().find(|p| p.0 == k).map(|p| p.1.clone());
        assert_eq!(q(newest, "start_hour").as_deref(), Some("2026-10-26T00:00"));
        assert_eq!(q(newest, "end_hour").as_deref(), Some("2026-10-26T05:00"));
        assert_eq!(q(newest, "models").as_deref(), Some("ecmwf_ifs"));
        // reanalysis: the days of October up to the 20th, and the newest two months
        let re: Vec<&str> = all
            .iter()
            .filter(|r| r.source == Source::Reanalysis)
            .map(|r| r.period.as_str())
            .collect();
        assert_eq!(re.len(), 22);
        assert_eq!(re[0], "2026-10-20");
        assert_eq!(re[19], "2026-10-01");
        assert_eq!(&re[20..], ["2026-09", "2026-08"]);
        // held periods are not asked for again, and the backfill moves on
        let mut held = Held::new();
        for r in &all {
            held.insert((r.source, r.tile.clone(), r.period.clone()));
        }
        // the same slot again: no space weather or forecast twice, only more backfill
        let again = due(slot, &s, std::slice::from_ref(&tile), &held, &GRIDS);
        let periods: Vec<&str> = again.iter().map(|r| r.period.as_str()).collect();
        assert_eq!(periods, ["2026-07", "2026-06"]);
        let next = due(
            slot + SLOT_MS,
            &s,
            std::slice::from_ref(&tile),
            &held,
            &GRIDS,
        );
        let periods: Vec<&str> = next.iter().map(|r| r.period.as_str()).collect();
        assert_eq!(
            periods,
            ["2026-10-26T18", "2026-10-26T06", "2026-07", "2026-06"]
        );
    }

    #[test]
    fn the_backfill_window_is_fixed_and_whole_months() {
        let chunks = reanalysis_chunks(at(2026, 10, 3, 0));
        // 6 days back is 27 September: September is not complete, so it goes by days
        assert_eq!(chunks[0].0, "2026-09-27");
        assert_eq!(chunks[26].0, "2026-09-01");
        assert_eq!(chunks[27].0, "2026-08");
        assert_eq!(chunks.last().unwrap().0, "2024-10");
        let months = chunks.iter().filter(|c| c.0.len() == 7).count();
        assert_eq!(months, 23); // Oct 2024 .. Aug 2026; October 2026 has no day yet
        for (_, first, last) in &chunks {
            assert!(first <= last);
        }
    }

    #[test]
    fn a_new_tile_waits_its_delay() {
        let s = schedule();
        let slot = at(2026, 10, 26, 12);
        let added = s.instant(slot) - s.delay("N50E030") + 1;
        let tile = Tile {
            name: "N50E030".into(),
            added_ms: added,
        };
        let held = Held::new();
        assert_eq!(
            due(slot, &s, std::slice::from_ref(&tile), &held, &GRIDS).len(),
            1
        );
        let later = due(slot + SLOT_MS, &s, &[tile], &held, &GRIDS);
        assert!(later.len() > 1);
    }
}
