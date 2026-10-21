//! This computer's facts for its ground station profile (docs/SPEC.md, "Ground
//! station profiles"): operating system, processor, memory, the disk that holds
//! the app's data, power source and clock synchronisation.
//!
//! Everything is read locally. The host name goes to the engine only at unlock,
//! which keeps a keyed hash of it and never the name; the facts sent with
//! `station.record_computer` carry no host name. A fact that cannot be read is
//! left out, never guessed.

use std::path::Path;
use std::process::Command;

use serde_json::{Map, Value, json};
use sysinfo::{CpuRefreshKind, Disks, MemoryRefreshKind, RefreshKind, System};

/// The computer's host name, for the engine's keyed hash.
pub fn host_name() -> Option<String> {
    System::host_name().filter(|h| !h.trim().is_empty())
}

/// The facts `station.record_computer` takes. `data_dir` picks the disk reported.
pub fn computer_facts(data_dir: &Path) -> Value {
    let system = System::new_with_specifics(
        RefreshKind::nothing()
            .with_cpu(CpuRefreshKind::nothing().with_frequency())
            .with_memory(MemoryRefreshKind::nothing().with_ram()),
    );
    let mut facts = Map::new();
    let mut put = |name: &str, value: Option<Value>| {
        if let Some(v) = value {
            facts.insert(name.into(), v);
        }
    };
    put("os", System::name().map(Value::from));
    put(
        "os_version",
        System::long_os_version()
            .or_else(System::os_version)
            .map(Value::from),
    );
    let cpu = system.cpus().first();
    put(
        "cpu",
        cpu.map(|c| c.brand().trim().to_owned())
            .filter(|b| !b.is_empty())
            .map(Value::from),
    );
    put(
        "cpu_mhz",
        cpu.map(|c| c.frequency())
            .filter(|f| *f > 0)
            .map(Value::from),
    );
    put("cpu_cores", System::physical_core_count().map(Value::from));
    put(
        "memory_bytes",
        Some(system.total_memory())
            .filter(|m| *m > 0)
            .map(Value::from),
    );
    if let Some((total, free)) = data_disk(data_dir) {
        put("disk_bytes", Some(json!(total)));
        put("disk_free_bytes", Some(json!(free)));
    }
    put("power", power_source().map(Value::from));
    let (synced, source) = clock();
    put("time_sync", synced.map(Value::from));
    put("time_source", source.map(Value::from));
    Value::Object(facts)
}

/// Total and free bytes of the disk that holds `dir`: the mount point that is
/// the longest prefix of it.
fn data_disk(dir: &Path) -> Option<(u64, u64)> {
    let dir = dir.canonicalize().unwrap_or_else(|_| dir.to_path_buf());
    let disks = Disks::new_with_refreshed_list();
    disks
        .list()
        .iter()
        .filter(|d| dir.starts_with(d.mount_point()))
        .max_by_key(|d| d.mount_point().components().count())
        .map(|d| (d.total_space(), d.available_space()))
}

/// "battery" or "mains", when the system says.
#[cfg(target_os = "linux")]
fn power_source() -> Option<&'static str> {
    let supplies = std::fs::read_dir("/sys/class/power_supply").ok()?;
    let mut battery = false;
    for supply in supplies.flatten() {
        let path = supply.path();
        let read = |name: &str| std::fs::read_to_string(path.join(name)).unwrap_or_default();
        match read("type").trim() {
            "Mains" if read("online").trim() == "1" => return Some("mains"),
            "Battery" => battery = true,
            _ => {}
        }
    }
    battery.then_some("battery")
}

#[cfg(windows)]
fn power_source() -> Option<&'static str> {
    use windows::Win32::System::Power::{GetSystemPowerStatus, SYSTEM_POWER_STATUS};
    let mut status = SYSTEM_POWER_STATUS::default();
    // SAFETY: GetSystemPowerStatus only writes the struct it is given.
    unsafe { GetSystemPowerStatus(&mut status) }.ok()?;
    match status.ACLineStatus {
        0 => Some("battery"),
        1 => Some("mains"),
        _ => None,
    }
}

#[cfg(not(any(target_os = "linux", windows)))]
fn power_source() -> Option<&'static str> {
    None
}

/// Whether the clock is synchronised, and from what.
#[cfg(target_os = "linux")]
fn clock() -> (Option<bool>, Option<String>) {
    let synced = run(
        "timedatectl",
        &["show", "--property=NTPSynchronized", "--value"],
    )
    .and_then(|out| match out.trim() {
        "yes" => Some(true),
        "no" => Some(false),
        _ => None,
    });
    (synced, synced.filter(|s| *s).map(|_| "NTP".to_owned()))
}

#[cfg(windows)]
fn clock() -> (Option<bool>, Option<String>) {
    // w32tm's labels are translated; only an English "Source:" line is read.
    let Some(out) = run("w32tm", &["/query", "/status"]) else {
        return (None, None);
    };
    parse_w32tm(&out)
}

#[cfg(not(any(target_os = "linux", windows)))]
fn clock() -> (Option<bool>, Option<String>) {
    (None, None)
}

/// The Windows time service's source: a local clock means not synchronised.
#[cfg_attr(not(windows), allow(dead_code))]
fn parse_w32tm(out: &str) -> (Option<bool>, Option<String>) {
    let source = out.lines().find_map(|line| {
        let (key, value) = line.split_once(':')?;
        (key.trim() == "Source").then(|| value.split(',').next().unwrap_or("").trim().to_owned())
    });
    match source {
        Some(s) if s.is_empty() => (None, None),
        Some(s) if s == "Local CMOS Clock" || s == "Free-running System Clock" => {
            (Some(false), Some(s))
        }
        Some(s) => (Some(true), Some(s)),
        None => (None, None),
    }
}

/// A local command's output, or None if it fails.
#[cfg_attr(not(any(target_os = "linux", windows)), allow(dead_code))]
fn run(program: &str, args: &[&str]) -> Option<String> {
    let mut command = Command::new(program);
    command.args(args);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    let out = command.output().ok()?;
    out.status
        .success()
        .then(|| String::from_utf8_lossy(&out.stdout).into_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_facts_are_well_formed_and_carry_no_host_name() {
        let facts = computer_facts(&std::env::temp_dir());
        let map = facts.as_object().unwrap();
        let known = [
            "os",
            "os_version",
            "cpu",
            "cpu_mhz",
            "cpu_cores",
            "memory_bytes",
            "disk_bytes",
            "disk_free_bytes",
            "power",
            "time_sync",
            "time_source",
        ];
        for (k, v) in map {
            assert!(known.contains(&k.as_str()), "unexpected fact {k}");
            assert!(!v.is_null(), "{k} is null");
        }
        assert!(map["memory_bytes"].as_u64().unwrap() > 0);
        if let Some(free) = map.get("disk_free_bytes") {
            assert!(free.as_u64().unwrap() <= map["disk_bytes"].as_u64().unwrap());
        }
        if let Some(power) = map.get("power") {
            assert!(["battery", "mains"].contains(&power.as_str().unwrap()));
        }
        assert!(!map.contains_key("host"));
        if let Some(host) = host_name().filter(|h| h.len() >= 8) {
            assert!(
                !facts.to_string().contains(&host),
                "the host name leaked into the facts"
            );
        }
    }

    #[test]
    fn the_windows_time_source_is_read_from_w32tm() {
        let synced = "Leap Indicator: 0(no warning)\r\nSource: time.windows.com,0x9\r\nPoll Interval: 10\r\n";
        assert_eq!(
            parse_w32tm(synced),
            (Some(true), Some("time.windows.com".into()))
        );
        let local = "Source: Local CMOS Clock\r\n";
        assert_eq!(
            parse_w32tm(local),
            (Some(false), Some("Local CMOS Clock".into()))
        );
        assert_eq!(parse_w32tm("Джерело: time.windows.com\r\n"), (None, None));
    }
}
