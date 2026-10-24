fn main() {
    // The Google OAuth client is built in from CI secrets (spec: Shared storage and
    // sync, "Setup"); a change to them must rebuild.
    println!("cargo:rerun-if-env-changed=FI_GOOGLE_CLIENT_ID");
    println!("cargo:rerun-if-env-changed=FI_GOOGLE_CLIENT_SECRET");
    tauri_build::build()
}
