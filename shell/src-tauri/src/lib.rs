//! Flight Insights desktop shell: windows, the engine sidecar, the OS keychain,
//! sync and outbound requests (see docs/SPEC.md, "System architecture").

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("error while running Flight Insights");
}
