# Setting up Google for sync

A one-time setup by a Google Workspace admin. It gives the app its sign-in (an Internal OAuth app and a desktop client), the fleet's shared drive, and the test drive for the nightly soak. SPEC "Shared storage and sync", "Setup", gives the reasons.

What you end up with:

| GitHub secret | What it is | Used by |
| --- | --- | --- |
| `FI_GOOGLE_CLIENT_ID` | the desktop OAuth client's ID | installers (sign-in) and the nightly soak |
| `FI_GOOGLE_CLIENT_SECRET` | that client's secret (not confidential for a desktop app, but kept out of the repo) | the same |
| `FI_GOOGLE_TEST_REFRESH_TOKEN` | the test account's sign-in, for the soak | the nightly soak |
| `FI_GOOGLE_TEST_DRIVE_ID` | the test shared drive's ID | the nightly soak |

You need a Workspace admin account for steps 1 to 4, and a machine with this repository and the Rust toolchain for step 6.

## 1. The Google Cloud project

1. Open [console.cloud.google.com](https://console.cloud.google.com) with a company account, and create a project, for example "Flight Insights". It must belong to the company's organization, not a personal account. Internal apps need that.
2. **APIs & Services → Library**: find "Google Drive API" and **Enable** it.

## 2. The OAuth app (Internal)

In **Google Auth Platform** (APIs & Services → OAuth consent screen):

1. **Branding**: an app name ("Flight Insights") and a support email.
2. **Audience**: user type **Internal**. Only company accounts can then sign in, and Google needs no verification.
3. **Data Access**: **Add or remove scopes** and add `https://www.googleapis.com/auth/drive`. This is the full Drive scope. The app needs it because each laptop reads objects the other laptops wrote, which the narrower `drive.file` scope would hide.

## 3. The desktop client

1. **Google Auth Platform → Clients → Create client**.
2. Application type: **Desktop app**. Name: "Flight Insights desktop".
3. Copy the **Client ID** and **Client secret** (or download the JSON). The app signs in through the system browser and a one-off listener on `127.0.0.1`, which Desktop clients allow without listing any redirect address.

## 4. The shared drives

In [Google Drive](https://drive.google.com) → **Shared drives → New**:

1. **The fleet's drive**, for example "Flight Insights fleet". Under **Manage members**, add each user's company account as **Contributor**. Contributors can add files but cannot move them to the trash. The app never deletes anything, and this keeps a mistaken or stolen laptop from deleting either.
2. **The test drive**, for example "Flight Insights soak test". It is for the nightly soak only and never holds real flight data.
   - Create (or pick) a **dedicated test account** in the company domain, for example `fi-soak@…`, and add it to the test drive as **Contributor**. Don't use a person's account: its sign-in will sit in GitHub's secrets.
   - Each soak run adds a folder `soak-<run>-<seed>` and deletes nothing. An admin (Manager) can clear old folders from time to time.

## 5. The client secrets in GitHub

In the repository: **Settings → Secrets and variables → Actions → New repository secret**:

- `FI_GOOGLE_CLIENT_ID`: the client ID from step 3.
- `FI_GOOGLE_CLIENT_SECRET`: the client secret from step 3.

The next run of the **Package** workflow (by hand, or on a `v*` tag) builds installers that can sign in. Until then, the app's Sync view says "Google sign-in is not set up in this build".

## 6. The test account's sign-in

On your own machine, never a shared one, with a browser on the same machine (Google sends the browser back to `127.0.0.1` there):

```sh
cd shell/src-tauri
FI_GOOGLE_CLIENT_ID='…' FI_GOOGLE_CLIENT_SECRET='…' cargo run --example google_sign_in
```

1. It prints an address. Open it in a browser **signed in as the test account** (a private window avoids picking the wrong account), and allow access.
2. It then prints the shared drives the account can see, with their IDs, and the account's refresh token. It writes nothing to disk.
3. Add two more repository secrets:
   - `FI_GOOGLE_TEST_DRIVE_ID`: the test drive's ID from that list (not the fleet's drive).
   - `FI_GOOGLE_TEST_REFRESH_TOKEN`: the refresh token.
4. Clear the terminal (and its scrollback). Don't paste the token anywhere else.

The token stays valid while the account exists and its access is not revoked. If a soak run reports "sign in again", run this step again. To revoke it: the test account's **Google Account → Security → Your connections to third-party apps**.

## 7. Check it

1. **Actions → Sync soak on the drive → Run workflow**, with the defaults. It should end with "sync soak clean" for each seed, and the test drive gains `soak-<run>-<seed>` folders.
2. After that it runs every night at 01:41 UTC. Clean runs on several nights in a row are the Phase 1 sync gate.
3. On a laptop with a new installer: **Sync → Sign in with Google**, choose the fleet's drive, and **Set up a new fleet** on the first laptop. The 24 recovery words go to an admin, who keeps them offline in two places. Other laptops then join from their own Sync view.

## Network

Laptops reach Google at `oauth2.googleapis.com` (tokens) and `www.googleapis.com` (Drive), and the browser opens `accounts.google.com` for sign-in. If a firewall or VPN filters traffic, allow these.
