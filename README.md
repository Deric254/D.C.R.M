# DericBI CRM

Find, contact and track leads. A Windows desktop app: a Tauri shell around a Python (FastAPI + SQLite) engine.

## How releases work

Every push to `main` runs the tests, builds the Windows installer and publishes it as a new version (`1.0.<build number>`; change the first two numbers in `VERSION`). Installed apps look for new versions on their own and show **Update ready** in the top bar. Install it from **Settings > About and updates**.

## One-time setup

Log in with `gh auth login`, then double-click `setup-release.bat`. It makes the signing key, puts the public key in `src-tauri/tauri.release.conf.json`, sets the `TAURI_SIGNING_PRIVATE_KEY` secret and pushes.

Installers are published as GitHub Releases on this repo, which must be public (an installed app cannot download from a private repo). No other secret is needed.

Manual version: run `npx tauri signer generate -w dericbi.key`, paste `dericbi.key.pub` over `REPLACE_WITH_PUBLIC_KEY` in `src-tauri/tauri.release.conf.json`, and add the contents of `dericbi.key` as the repo secret `TAURI_SIGNING_PRIVATE_KEY` (Settings > Secrets and variables > Actions). Add `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` only if you set a password. Never commit the key (`.gitignore` blocks `*.key`). Losing it means installed apps can no longer update.

The workflow stops with a clear message if the key or public key is missing.

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
