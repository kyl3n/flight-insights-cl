//! Signs a Google account in once, for the nightly real-drive soak (WP19): prints the
//! shared drives it can see and its refresh token, for the GitHub secrets
//! FI_GOOGLE_TEST_DRIVE_ID and FI_GOOGLE_TEST_REFRESH_TOKEN. Run it on your own machine
//! with the test account, never on a shared one (docs/google-setup.md, step 6).
//!
//! The "Google sign-in helper" workflow builds it with the desktop OAuth client built
//! in, to download and run as it is. From a checkout, give it the client instead:
//!
//!     FI_GOOGLE_CLIENT_ID=... FI_GOOGLE_CLIENT_SECRET=... cargo run --example google_sign_in
//!
//! Nothing is written to disk. The token is the only thing on stdout, so it can be piped
//! straight into `gh secret set`; everything else goes to stderr. In a window of its
//! own it waits for Enter before closing, so the token or an error stays readable. Paste
//! the token into the secret, then close the window. It can be revoked any time from
//! the account's Google security page.

use std::io::IsTerminal;
use std::time::Duration;

use flight_insights_shell::sync::google::shared_drives;
use flight_insights_shell::sync::oauth::{self, Client, Endpoints};

fn main() {
    let from_env = match (
        std::env::var("FI_GOOGLE_CLIENT_ID"),
        std::env::var("FI_GOOGLE_CLIENT_SECRET"),
    ) {
        (Ok(id), Ok(secret)) if !id.is_empty() => Some(Client { id, secret }),
        _ => None,
    };
    let Some(client) = from_env.or_else(oauth::built_in_client) else {
        eprintln!(
            "This build has no Google client: set FI_GOOGLE_CLIENT_ID and \
             FI_GOOGLE_CLIENT_SECRET to the desktop OAuth client first."
        );
        finish(2);
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
            finish(1);
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
        "\nFI_GOOGLE_TEST_REFRESH_TOKEN (paste it into the GitHub secret, then close this window):"
    );
    println!("{}", signed.refresh.as_str());
    drop(signed); // zeroes the token; exit() runs no destructors
    finish(0);
}

/// Exits, first waiting for Enter when a person is reading the output in a window that
/// closes with the program (double-clicked on Windows). It exits at once in CI, and when
/// the token is piped into `gh secret set`, which waits for the end of the input.
fn finish(code: i32) -> ! {
    let window = std::io::stdin().is_terminal() && std::io::stderr().is_terminal();
    let token_piped = code == 0 && !std::io::stdout().is_terminal();
    if window && !token_piped {
        eprint!("\nPress Enter to close.");
        let _ = std::io::stdin().read_line(&mut String::new());
    }
    std::process::exit(code)
}
