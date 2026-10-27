//! One slot's downloads (docs/SPEC.md, "Weather and wind"): the engine names the tiles,
//! the plan says what is due, each request goes out through `net`, and each reply is
//! handed to the engine to keep. The Open-Meteo key is added to a request as it is sent
//! and is never in a log line or an error; errors never carry the URL.

use std::fs;
use std::path::Path;

use serde_json::{Value, json};

use crate::claude::Engine;
use crate::net::{self, Client, StatusCode, Url};
use crate::weather::plan::{self, Grids, Held, Request, Schedule, Source, Tile};

/// Where each source is asked. Tests point these at a local stand-in.
#[derive(Debug, Clone)]
pub struct Hosts {
    pub forecast: String,
    pub archive: String,
    pub swpc: String,
}

impl Default for Hosts {
    fn default() -> Self {
        Hosts {
            forecast: "https://customer-api.open-meteo.com".into(),
            archive: "https://customer-archive-api.open-meteo.com".into(),
            swpc: "https://services.swpc.noaa.gov".into(),
        }
    }
}

/// The largest reply kept: the engine refuses more.
const MAX_REPLY: u64 = 64 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq)]
pub enum Stop {
    /// No connection: tried again at the next slot.
    Offline,
    /// Open-Meteo refused the key.
    KeyRefused,
    /// The key is good, but its plan does not cover this endpoint: Open-Meteo's reason.
    PlanLacks(String),
    /// The provider is busy or failing: tried again at the next slot.
    Busy(&'static str),
    /// Anything else, for the status line.
    Failed(String),
}

impl std::fmt::Display for Stop {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Stop::Offline => write!(f, "no connection"),
            Stop::KeyRefused => write!(f, "Open-Meteo refused the key"),
            Stop::PlanLacks(why) => write!(f, "the Open-Meteo plan does not cover this: {why}"),
            Stop::Busy(who) => write!(f, "{who} is busy; tried again at the next download time"),
            Stop::Failed(why) => write!(f, "{why}"),
        }
    }
}

#[derive(Debug, Default, Clone, PartialEq)]
pub struct Round {
    pub tiles: usize,
    pub due: usize,
    pub kept: usize,
    /// Of those kept: found on the fleet's drive, and fetched here and shared.
    pub taken: usize,
    pub given: usize,
}

/// The fleet's shared downloads, when this laptop is in one.
pub trait Share {
    /// Keeps a download another laptop shared, when there is one that holds; whether
    /// it did.
    fn take(&self, engine: &dyn Engine, r: &Request) -> Result<bool, String>;
    /// Shares a download kept here.
    fn give(&self, engine: &dyn Engine, r: &Request) -> Result<(), String>;
}

pub(crate) fn tiles_and_grids(engine: &dyn Engine) -> Result<(Vec<Tile>, Grids), Stop> {
    let got = engine
        .call("weather.tiles", json!({}))
        .map_err(Stop::Failed)?;
    let bad = || Stop::Failed("the engine's tile list is not readable".into());
    let tiles = got["tiles"]
        .as_array()
        .ok_or_else(bad)?
        .iter()
        .map(|t| {
            Some(Tile {
                name: t["tile"].as_str()?.to_owned(),
                added_ms: t["added_ms"].as_i64()?,
            })
        })
        .collect::<Option<Vec<_>>>()
        .ok_or_else(bad)?;
    let grids = Grids {
        tile_deg: got["tile_deg"].as_i64().ok_or_else(bad)?,
        forecast_deg: got["grids_deg"]["forecast"].as_f64().ok_or_else(bad)?,
        reanalysis_deg: got["grids_deg"]["reanalysis"].as_f64().ok_or_else(bad)?,
    };
    Ok((tiles, grids))
}

fn held(engine: &dyn Engine, source: Source, since: &str) -> Result<Vec<(String, String)>, Stop> {
    let got = engine
        .call(
            "weather.held",
            json!({"source": source.name(), "since": since}),
        )
        .map_err(Stop::Failed)?;
    Ok(got["held"]
        .as_array()
        .map(|rows| {
            rows.iter()
                .filter_map(|r| Some((r[0].as_str()?.to_owned(), r[1].as_str()?.to_owned())))
                .collect()
        })
        .unwrap_or_default())
}

/// The address a request goes to, with the key for Open-Meteo.
pub(crate) fn address(hosts: &Hosts, r: &Request, key: &str) -> Result<Url, Stop> {
    let (base, path) = match r.source {
        Source::Forecast => (&hosts.forecast, "/v1/forecast"),
        Source::Reanalysis => (&hosts.archive, "/v1/archive"),
        Source::Kp => (&hosts.swpc, "/products/noaa-planetary-k-index.json"),
    };
    let mut url = Url::parse(&format!("{base}{path}"))
        .map_err(|_| Stop::Failed("a weather address is not valid".into()))?;
    if r.source != Source::Kp {
        let mut q = url.query_pairs_mut();
        for (k, v) in &r.query {
            q.append_pair(k, v);
        }
        q.append_pair("apikey", key);
    }
    net::allowed(&url).map_err(Stop::Failed)?;
    Ok(url)
}

/// One request: its reply, or why the round stops.
pub(crate) fn fetch(http: &Client, url: Url, who: &'static str) -> Result<Vec<u8>, Stop> {
    let key = url
        .query_pairs()
        .find(|(k, _)| k == "apikey")
        .map(|(_, v)| v.into_owned());
    let reply = http.get(url).send().map_err(|e| {
        if e.is_connect() || e.is_timeout() {
            Stop::Offline
        } else {
            // without_url: the URL holds the key
            Stop::Failed(format!("{who} could not be reached: {}", e.without_url()))
        }
    })?;
    let status = reply.status();
    if status == StatusCode::UNAUTHORIZED || status == StatusCode::FORBIDDEN {
        if who != "Open-Meteo" {
            return Err(Stop::Failed(format!("{who} answered {status}")));
        }
        // A plan that does not cover an endpoint is a 403 whose reason says so; the
        // key itself is good. The reason is Open-Meteo's text, with the key masked.
        let reason = reply
            .text()
            .ok()
            .and_then(|t| serde_json::from_str::<Value>(&t).ok())
            .and_then(|v| v["reason"].as_str().map(str::to_owned))
            .unwrap_or_default();
        if status == StatusCode::FORBIDDEN && reason.to_lowercase().contains("plan") {
            let mut why: String = reason.chars().take(200).collect();
            if let Some(key) = key.filter(|k| !k.is_empty()) {
                why = why.replace(key.as_str(), "***");
            }
            return Err(Stop::PlanLacks(why));
        }
        return Err(Stop::KeyRefused);
    }
    if status == StatusCode::TOO_MANY_REQUESTS || status.is_server_error() {
        return Err(Stop::Busy(who));
    }
    if !status.is_success() {
        return Err(Stop::Failed(format!("{who} answered {status}")));
    }
    if reply.content_length().is_some_and(|n| n > MAX_REPLY) {
        return Err(Stop::Failed(format!("{who}'s reply is too large")));
    }
    let bytes = reply
        .bytes()
        .map_err(|e| Stop::Failed(format!("{who}'s reply was cut off: {}", e.without_url())))?;
    if bytes.len() as u64 > MAX_REPLY {
        return Err(Stop::Failed(format!("{who}'s reply is too large")));
    }
    Ok(bytes.to_vec())
}

/// Everything due at the slot starting at `slot`: sent in turn, each reply kept by the
/// engine. The first request that cannot be made stops the round; what is left is due
/// again at the next slot.
#[allow(clippy::too_many_arguments)]
pub fn round(
    engine: &dyn Engine,
    http: &Client,
    hosts: &Hosts,
    key: &str,
    schedule: &Schedule,
    slot: i64,
    staging: &Path,
    sent: &mut dyn FnMut(&Request),
    share: Option<&dyn Share>,
) -> Result<Round, Stop> {
    let (tiles, grids) = tiles_and_grids(engine)?;
    let mut have = Held::new();
    let since = [
        (Source::Forecast, plan::hour_label(slot - 6 * plan::DAY_MS)),
        (
            Source::Reanalysis,
            plan::day_label(slot.div_euclid(plan::DAY_MS) - 800)[..7].to_owned(),
        ),
        (Source::Kp, plan::hour_label(slot)),
    ];
    for (source, from) in since {
        for (tile, period) in held(engine, source, &from)? {
            have.insert((source, tile, period));
        }
    }
    let due = plan::due(slot, schedule, &tiles, &have, &grids);
    let mut out = Round {
        tiles: tiles.len(),
        due: due.len(),
        ..Round::default()
    };
    fs::create_dir_all(staging).map_err(|e| Stop::Failed(e.to_string()))?;
    let unshared = |e: String| Stop::Failed(format!("the fleet's drive: {e}"));
    for r in &due {
        if let Some(fleet) = share
            && fleet.take(engine, r).map_err(unshared)?
        {
            out.kept += 1;
            out.taken += 1;
            continue;
        }
        let who = if r.source == Source::Kp {
            "NOAA SWPC"
        } else {
            "Open-Meteo"
        };
        let body = fetch(http, address(hosts, r, key)?, who)?;
        sent(r);
        let file = staging.join(format!("{}-{}.json", r.source.name(), out.kept));
        fs::write(&file, &body).map_err(|e| Stop::Failed(e.to_string()))?;
        let stored = engine.call(
            "weather.store",
            json!({
                "source": r.source.name(),
                "tile": r.tile,
                "period": r.period,
                "path": file.to_string_lossy(),
            }),
        );
        let _ = fs::remove_file(&file);
        stored.map_err(|e| Stop::Failed(format!("{who}'s reply was not kept: {e}")))?;
        out.kept += 1;
        if let Some(fleet) = share {
            fleet.give(engine, r).map_err(unshared)?;
            out.given += 1;
        }
    }
    Ok(out)
}

/// For the status line: the round's outcome without anything that names a place.
pub fn summary(result: &Result<Round, Stop>) -> Value {
    match result {
        Ok(r) => json!({
            "state": "done", "tiles": r.tiles, "kept": r.kept,
            "from_fleet": r.taken, "shared": r.given,
        }),
        Err(Stop::Offline) => json!({"state": "offline"}),
        Err(Stop::KeyRefused) => json!({"state": "key refused"}),
        Err(e @ Stop::PlanLacks(_)) => json!({"state": "plan", "why": e.to_string()}),
        Err(e @ Stop::Busy(_)) => json!({"state": "busy", "why": e.to_string()}),
        Err(e) => json!({"state": "failed", "why": e.to_string()}),
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::io::{BufRead, BufReader, Write};
    use std::net::TcpListener;
    use std::sync::{Arc, Mutex};
    use std::time::Duration;

    use crate::weather::plan::{DAY_MS, HOUR_MS, SLOT_MS, days};

    pub(crate) const KEY: &str = "om-test-key-0123456789";

    /// A local stand-in for Open-Meteo and NOAA: answers every request with a canned
    /// reply by path, or `status`, and keeps each request line.
    pub(crate) struct FakeWeather {
        pub url: String,
        pub seen: Arc<Mutex<Vec<String>>>,
        pub status: Arc<Mutex<u16>>,
    }

    pub(crate) fn fake_weather() -> FakeWeather {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let seen: Arc<Mutex<Vec<String>>> = Arc::default();
        let status = Arc::new(Mutex::new(200u16));
        let (log, code) = (seen.clone(), status.clone());
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(stream) = stream else { return };
                let mut reader = BufReader::new(stream.try_clone().unwrap());
                let mut first = String::new();
                reader.read_line(&mut first).unwrap();
                loop {
                    let mut line = String::new();
                    reader.read_line(&mut line).unwrap();
                    if line.trim_end().is_empty() {
                        break;
                    }
                }
                let target = first.split(' ').nth(1).unwrap_or_default().to_owned();
                log.lock().unwrap().push(target.clone());
                let s = *code.lock().unwrap();
                let body = if target.starts_with("/products/") {
                    r#"[["time_tag","Kp"],["2026-10-26 00:00:00.000","2.33"]]"#
                } else if s == 403 && target.starts_with("/v1/archive") {
                    // Open-Meteo's answer when the plan lacks the endpoint
                    r#"{"error":true,"reason":"This API endpoint requires the API Professional or Enterprise plan"}"#
                } else {
                    r#"{"latitude":50.0,"longitude":30.0,"hourly":{"time":[]}}"#
                };
                let mut out = stream;
                let _ = write!(
                    out,
                    "HTTP/1.1 {s} X\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
                    body.len()
                );
            }
        });
        FakeWeather { url, seen, status }
    }

    /// The engine's side: a tile list, and what it keeps.
    #[derive(Default)]
    pub(crate) struct FakeEngine {
        pub tiles: Vec<(String, i64)>,
        pub kept: Mutex<HashMap<(String, String, String), Vec<u8>>>,
    }

    impl Engine for FakeEngine {
        fn call(&self, method: &str, params: Value) -> Result<Value, String> {
            match method {
                "weather.tiles" => Ok(json!({
                    "tile_deg": 2,
                    "grids_deg": {"forecast": 0.1, "reanalysis": 0.25},
                    "tiles": self.tiles.iter()
                        .map(|(t, ms)| json!({"tile": t, "added_ms": ms}))
                        .collect::<Vec<_>>(),
                })),
                "weather.held" => {
                    let source = params["source"].as_str().unwrap();
                    let held: Vec<Value> = self
                        .kept
                        .lock()
                        .unwrap()
                        .keys()
                        .filter(|k| k.0 == source)
                        .map(|k| json!([k.1, k.2]))
                        .collect();
                    Ok(json!({ "held": held }))
                }
                "weather.store" => {
                    let path = params["path"].as_str().unwrap();
                    let body = fs::read(path).map_err(|e| e.to_string())?;
                    let key = |k: &str| params[k].as_str().unwrap().to_owned();
                    self.kept
                        .lock()
                        .unwrap()
                        .insert((key("source"), key("tile"), key("period")), body);
                    Ok(json!({"bytes": 1}))
                }
                "weather.export" => {
                    let key = |k: &str| params[k].as_str().unwrap().to_owned();
                    let kept = self.kept.lock().unwrap();
                    let body = kept
                        .get(&(key("source"), key("tile"), key("period")))
                        .ok_or("that download is not kept here")?;
                    fs::write(params["path"].as_str().unwrap(), body).map_err(|e| e.to_string())?;
                    Ok(json!({"bytes": body.len()}))
                }
                other => Err(format!("unexpected {other}")),
            }
        }
    }

    pub(crate) fn hosts(base: &str) -> Hosts {
        Hosts {
            forecast: base.into(),
            archive: base.into(),
            swpc: base.into(),
        }
    }

    fn schedule() -> Schedule {
        Schedule {
            offset_min: 23,
            seed: [9; 32],
        }
    }

    pub(crate) fn staging() -> std::path::PathBuf {
        std::env::temp_dir().join(format!(
            "fi-weather-{}",
            hex::encode(crate::sync::sodium::random::<6>().as_ref())
        ))
    }

    fn query(target: &str) -> Vec<(String, String)> {
        let url = Url::parse(&format!("http://x{target}")).unwrap();
        url.query_pairs().into_owned().collect()
    }

    /// A date or an hour a request names: whole days, or a slot window's first hour
    /// (a multiple of 6) and last (5 past one).
    fn date_ok(k: &str, v: &str) -> bool {
        let day = |d: &str| {
            let p: Vec<&str> = d.split('-').collect();
            p.len() == 3 && p.iter().all(|x| x.parse::<u32>().is_ok())
        };
        match v.split_once('T') {
            None => k.ends_with("_date") && day(v),
            Some((d, t)) => {
                let hour: Option<u32> = t.strip_suffix(":00").and_then(|h| h.parse().ok());
                day(d)
                    && match (k, hour) {
                        ("start_hour", Some(h)) => h % 6 == 0,
                        ("end_hour", Some(h)) => h % 6 == 5,
                        _ => false,
                    }
            }
        }
    }

    fn run_days(engine: &FakeEngine, fake: &FakeWeather, start: i64, n_slots: i64) -> Vec<String> {
        let http = net::client(Duration::from_secs(10)).unwrap();
        let s = schedule();
        let dir = staging();
        for k in 0..n_slots {
            let slot = start + k * SLOT_MS;
            round(
                engine,
                &http,
                &hosts(&fake.url),
                KEY,
                &s,
                slot,
                &dir,
                &mut |_| {},
                None,
            )
            .unwrap();
        }
        fake.seen.lock().unwrap().clone()
    }

    #[test]
    fn every_number_in_a_request_is_a_grid_point_or_a_slot_aligned_date() {
        let fake = fake_weather();
        let engine = FakeEngine {
            tiles: vec![("N50E030".into(), 0), ("S34W060".into(), 0)],
            ..Default::default()
        };
        let start = days(2026, 10, 24) * DAY_MS;
        let seen = run_days(&engine, &fake, start, 8);
        // the two tiles' points, every 0.1 and every 0.25 degrees, in hundredths
        let points = |a: i64, b: i64| -> std::collections::HashSet<String> {
            (0..20)
                .flat_map(|i| [a + i * 10, b + i * 10])
                .chain((0..8).flat_map(|i| [a + i * 25, b + i * 25]))
                .map(plan::hundredths)
                .collect()
        };
        let (grid_lat, grid_lon) = (points(5000, -3400), points(3000, -6000));
        let fixed = [
            ("hourly", plan::FORECAST_HOURLY.join(",")),
            ("hourly", plan::REANALYSIS_HOURLY.join(",")),
            ("models", "ecmwf_ifs".to_owned()),
            ("models", "era5".to_owned()),
            ("cell_selection", "nearest".to_owned()),
            ("timezone", "GMT".to_owned()),
            ("wind_speed_unit", "ms".to_owned()),
        ];
        let mut kp = 0;
        for target in &seen {
            if target.starts_with("/products/") {
                assert_eq!(
                    target, "/products/noaa-planetary-k-index.json",
                    "no parameters"
                );
                kp += 1;
                continue;
            }
            for (k, v) in query(target) {
                match k.as_str() {
                    "latitude" => assert!(v.split(',').all(|x| grid_lat.contains(x)), "{v}"),
                    "longitude" => assert!(v.split(',').all(|x| grid_lon.contains(x)), "{v}"),
                    "start_hour" | "end_hour" | "start_date" | "end_date" => {
                        assert!(date_ok(&k, &v), "{k}={v}")
                    }
                    "apikey" => assert_eq!(v, KEY),
                    _ => assert!(fixed.contains(&(k.as_str(), v.clone())), "{k}={v}"),
                }
            }
        }
        assert_eq!(kp, 8, "one space weather request a slot");
        // every reply is kept, each under its own source, tile and period
        assert_eq!(engine.kept.lock().unwrap().len(), seen.len());
    }

    #[test]
    fn the_requests_depend_on_the_tiles_and_the_clock_alone() {
        // The engine's tile list is all the shell learns of the flights: two fleets with
        // the same tiles, one with many more flights in them, ask for the same things.
        let start = days(2026, 10, 24) * DAY_MS;
        let logs: Vec<Vec<String>> = (0..2)
            .map(|_| {
                let fake = fake_weather();
                let engine = FakeEngine {
                    tiles: vec![("N50E030".into(), 0)],
                    ..Default::default()
                };
                run_days(&engine, &fake, start, 6)
            })
            .collect();
        assert_eq!(logs[0], logs[1]);
        assert!(!logs[0].is_empty());
    }

    #[test]
    fn a_new_tile_is_not_named_before_its_delay() {
        let s = schedule();
        let start = days(2026, 10, 24) * DAY_MS;
        let added = start - HOUR_MS;
        let fake = fake_weather();
        let engine = FakeEngine {
            tiles: vec![("N50E030".into(), added)],
            ..Default::default()
        };
        let http = net::client(Duration::from_secs(10)).unwrap();
        let dir = staging();
        let mut first_named = None;
        for k in 0..16 {
            let slot = start + k * SLOT_MS;
            let mut named = false;
            round(
                &engine,
                &http,
                &hosts(&fake.url),
                KEY,
                &s,
                slot,
                &dir,
                &mut |r| {
                    named |= r.tile == "N50E030";
                },
                None,
            )
            .unwrap();
            if named && first_named.is_none() {
                first_named = Some(slot);
            }
        }
        let first = first_named.expect("named within four days");
        assert!(s.instant(first) >= added + s.delay("N50E030"));
        assert!(s.instant(first - SLOT_MS) < added + s.delay("N50E030"));
    }

    #[test]
    fn errors_never_show_the_key() {
        let fake = fake_weather();
        let http = net::client(Duration::from_secs(10)).unwrap();
        let url = Url::parse(&format!("{}/v1/forecast?apikey={KEY}", fake.url)).unwrap();
        for (code, who, want) in [
            (401, "Open-Meteo", Stop::KeyRefused),
            (403, "Open-Meteo", Stop::KeyRefused),
            (
                401,
                "NOAA SWPC",
                Stop::Failed("NOAA SWPC answered 401 Unauthorized".into()),
            ),
            (429, "Open-Meteo", Stop::Busy("Open-Meteo")),
            (503, "NOAA SWPC", Stop::Busy("NOAA SWPC")),
            (
                400,
                "Open-Meteo",
                Stop::Failed("Open-Meteo answered 400 Bad Request".into()),
            ),
        ] {
            *fake.status.lock().unwrap() = code;
            let got = fetch(&http, url.clone(), who).unwrap_err();
            assert_eq!(got, want);
            assert!(!got.to_string().contains(KEY));
            assert!(!summary(&Err(got)).to_string().contains(KEY));
        }
        // a plan without the archive is not a refused key, and says what it lacks
        *fake.status.lock().unwrap() = 403;
        let archive = Url::parse(&format!("{}/v1/archive?apikey={KEY}", fake.url)).unwrap();
        let got = fetch(&http, archive, "Open-Meteo").unwrap_err();
        assert_eq!(
            got,
            Stop::PlanLacks(
                "This API endpoint requires the API Professional or Enterprise plan".into()
            )
        );
        assert_eq!(summary(&Err(got.clone()))["state"], "plan");
        assert!(!summary(&Err(got)).to_string().contains(KEY));
        // nothing listens: no connection, and nothing about the request in the error
        let engine = FakeEngine {
            tiles: vec![("N50E030".into(), 0)],
            ..Default::default()
        };
        let slot = days(2026, 10, 24) * DAY_MS;
        let gone = hosts("http://127.0.0.1:9");
        let got = round(
            &engine,
            &http,
            &gone,
            KEY,
            &schedule(),
            slot,
            &staging(),
            &mut |_| {},
            None,
        );
        assert_eq!(got, Err(Stop::Offline));
    }

    #[test]
    fn only_allowed_hosts_are_asked() {
        let engine = FakeEngine {
            tiles: vec![("N50E030".into(), 0)],
            ..Default::default()
        };
        let http = net::client(Duration::from_secs(10)).unwrap();
        let slot = days(2026, 10, 24) * DAY_MS;
        let evil = hosts("https://weather.example.com");
        let got = round(
            &engine,
            &http,
            &evil,
            KEY,
            &schedule(),
            slot,
            &staging(),
            &mut |_| {},
            None,
        );
        assert!(matches!(got, Err(Stop::Failed(m)) if m.contains("not an endpoint")));
        for host in [
            Hosts::default().forecast,
            Hosts::default().archive,
            Hosts::default().swpc,
        ] {
            assert!(net::allowed(&Url::parse(&host).unwrap()).is_ok(), "{host}");
        }
    }
}
