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
- **Program icon (exe, installer, taskbar):** replace `backend/static/logo.png` with a square PNG, 512 px or larger, and push. Every icon size is made from it automatically: on each release build and each time you run `dev.bat` or `build-installer.bat`.

## Ask the AI (Find leads)

On **Find leads**, describe who you want to reach in your own words, for example "pharmacies and clinics in Kisii and Migori that could use my billing software". The AI suggests business-type and town searches; you can talk it through over several messages, remove any you don't want, then **Add to my searches** or **Add and start finding leads**. Nothing runs until you press a start button. It only suggests what to search for: the leads themselves always come from the real finder, never from the AI. It uses the free AI key from Settings > AI writing help.

## AI writing help

Settings > AI writing help takes free keys from Google Gemini, Groq, NVIDIA, OpenRouter, Mistral, or any OpenAI-compatible server (such as Ollama). Keys are tried in order, so when one hits its free limit the next one answers. **Write with AI** appears when you write a campaign and when you message one lead (it replies to their latest message). Free-tier model names change now and then: if **Test the keys** says a model isn't found, type a current model name in the model field.

## AI campaigns (Outreach)

On **Outreach**, tick **Let the AI write each message for each person**. The message box becomes a brief (the goal, the offer, what you want them to do) and the AI writes every lead's own message just before it is sent, from everything you know about that lead: business, town, address, website, status, your notes and the conversation so far. Emails and texts go out through the same sender as any campaign, with the same daily limits, sending hours, opt-out line and do-not-contact rules.

- **Set up with AI** at the top of the form turns a sentence like "email the pharmacies in Meru about my billing software" into a filled-in campaign (channel, audience, brief). You check it, then press Start. Nothing is sent until you do.
- **Show example messages** drafts the first few so you can judge the writing before starting. They aren't kept; each real message is written fresh.
- **No repeats:** nobody is messaged twice in a campaign or again within your skip-days, and a message that comes out nearly identical to another in the campaign is reworded once, then held back (shown as Failed; **Retry failed** writes it again).
- **Never half-written:** a message with a leftover `{blank}` or no email subject is not sent. If the AI is busy or out of free limit the message waits and retries; if no AI key is set the campaign pauses.
- **The log:** the exact text of every message is kept. Open a campaign's **Details** to read what each lead got, or open the lead to see it in their history.

## Local use

Building the installer on your own PC needs Python, Node.js, Rust and the **Visual Studio Build Tools** (workload “Desktop development with C++”). GitHub builds it for you on every push, so you only need these to build locally.


- `dev.bat` runs the desktop app from source.
- `start-in-browser.bat` runs it in the browser (no updater).
- `build-installer.bat` builds an installer locally.
- `python tests/test_backend.py` runs the backend tests.

Data lives in `%APPDATA%\DericBI-CRM` and survives updates.
