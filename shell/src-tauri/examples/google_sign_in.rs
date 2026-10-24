//! Signs a Google account in once, for the nightly real-drive soak (WP19): prints the
//! shared drives it can see and its refresh token, for the GitHub secrets
//! FI_GOOGLE_TEST_DRIVE_ID and FI_GOOGLE_TEST_REFRESH_TOKEN. Run it on your own machine
//! with the test account, never on a shared one:
//!
//!     FI_GOOGLE_CLIENT_ID=... FI_GOOGLE_CLIENT_SECRET=... cargo run --example google_sign_in
//!
//! Nothing is written to disk. The token is printed once, to this terminal only; paste
//! it into the secret and clear the terminal. It can be revoked any time from the
//! account's Google security page.

use std::time::Duration;

use flight_insights_shell::sync::google::shared_drives;
use flight_insights_shell::sync::oauth::{self, Client, Endpoints};

fn main() {
    let client = match (
        std::env::var("FI_GOOGLE_CLIENT_ID"),
        std::env::var("FI_GOOGLE_CLIENT_SECRET"),
    ) {
        (Ok(id), Ok(secret)) if !id.is_empty() => Client { id, secret },
        _ => {
            eprintln!(
                "Set FI_GOOGLE_CLIENT_ID and FI_GOOGLE_CLIENT_SECRET to the desktop OAuth client first."
            );
            std::process::exit(2);
        }
    };
    let endpoints = Endpoints::default();
    let open = |url: &str| {
        eprintln!("Open this address in a browser signed in as the test account:\n\n{url}\n");
        Ok(())
    };
    let signed = match oauth::sign_in(&endpoints, &client, &open, Duration::from_secs(600)) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("The sign-in did not complete: {e}");
            std::process::exit(1);
        }
    };
    match shared_drives(&endpoints, &client, &signed.refresh) {
        Ok(drives) => {
            eprintln!("Shared drives this account can see (FI_GOOGLE_TEST_DRIVE_ID is the id):");
            for (id, name) in drives {
                eprintln!("  {id}  {name}");
            }
        }
        Err(e) => eprintln!("The shared drives could not be listed: {e}"),
    }
    eprintln!(
        "\nFI_GOOGLE_TEST_REFRESH_TOKEN (paste it into the GitHub secret, then clear this terminal):"
    );
    println!("{}", signed.refresh.as_str());
}
