# DericBI CRM

Find, contact and track leads. A Windows desktop app: a Tauri shell around a Python (FastAPI + SQLite) engine.

## How releases work

Every push to `main` runs the tests, builds the Windows installer and publishes it as a new version (`1.0.<build number>`; change the first two numbers in `VERSION`). Installed apps look for new versions on their own and show **Update ready** in the top bar. Install it from **Settings > About and updates**.

## One-time setup

1. **Releases repo.** Create a public, empty repo `Deric254/D.C.R.M-releases`. This code repo is private, and an installed app cannot download from a private repo, so installers are published there.
2. **Signing keys.** Run `npx tauri signer generate -w dericbi.key`. Keep `dericbi.key` safe and never commit it (`.gitignore` blocks `*.key`). Losing it means installed apps can no longer update.
3. **Public key.** Paste the contents of `dericbi.key.pub` into `src-tauri/tauri.release.conf.json`, replacing `REPLACE_WITH_PUBLIC_KEY`.
4. **GitHub secrets** (this repo > Settings > Secrets and variables > Actions):
   - `TAURI_SIGNING_PRIVATE_KEY`: contents of `dericbi.key`
   - `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`: the password you chose (leave empty if none)
   - `RELEASES_TOKEN`: a personal access token (fine-grained, contents read and write on the releases repo only)

The workflow stops with a clear message if any of this is missing.

## Logo and slogan

- **Inside the app:** Settings > Logo and slogan. Upload a new logo or change the slogan any time.
- **Program icon (exe, installer, taskbar):** replace `backend/static/logo.png` with a square PNG, 512 px or larger, and push. The build makes every icon size from it. To see it in local runs too, double-click `set-logo.bat`.

## AI writing help

Settings > AI writing help takes free keys from Google Gemini, Groq, NVIDIA, OpenRouter, Mistral, or any OpenAI-compatible server (such as Ollama). Keys are tried in order, so when one hits its free limit the next one answers. **Write with AI** appears when you write a campaign and when you message one lead (it replies to their latest message). Free-tier model names change now and then: if **Test the keys** says a model isn't found, type a current model name in the model field.

## Local use

Building the installer on your own PC needs Python, Node.js, Rust and the **Visual Studio Build Tools** (workload “Desktop development with C++”). GitHub builds it for you on every push, so you only need these to build locally.


- `dev.bat` runs the desktop app from source.
- `start-in-browser.bat` runs it in the browser (no updater).
- `build-installer.bat` builds an installer locally.
- `python tests/test_backend.py` runs the backend tests.

Data lives in `%APPDATA%\DericBI-CRM` and survives updates.
