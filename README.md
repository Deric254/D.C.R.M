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

Settings > AI writing help takes free keys from Google Gemini, Groq, NVIDIA, OpenRouter, Mistral, or any OpenAI-compatible server (such as Ollama). Keys are tried in order, so when one hits its free limit the next one answers. **Write with AI** appears when you write a campaign and when you message one lead (it replies to their latest message). Free-tier model names get retired every few months. The app tries a list of current models for each provider and, if they are all gone, asks the provider which models it offers today and uses one that works (**Test the keys** shows which). You can still type a model name of your own. Settings also has one-click presets for Gmail/Outlook/Zoho/Yahoo and the Africa's Talking sandbox, plus step-by-step guides for getting the passwords and keys.

## WhatsApp, email and text from one lead

Open a lead and press **WhatsApp**, **Email** or **Text**. The AI writes the message straight away from the lead's business, town and type of business, your notes, and the whole conversation so far: a first hello that introduces you and your offer, an answer to something they said, or a follow-up that refers to your earlier message when they have not replied (a different angle each time, and a gracious last note after three). Edit it or press **Write with AI** for another version.

- **WhatsApp is free:** **Open WhatsApp** logs the message in the lead's history and opens the chat with the text ready; you press send. Log their answer with **Log a reply > WhatsApp**.
- **Everything is tracked:** each reply is matched to the message of ours it answers ("In answer to our WhatsApp of ..."), a lead you are still waiting on shows **Awaiting reply**, and Overview shows how many leads answered on each channel.
- **Your identity:** Settings > Sender name and Settings > AI writing help > *Who you are and what you sell* decide how the AI introduces you (default: Deric Marangu, data analyst, with ready-made tools adapted to each business). Edit that text to name your actual tools.

## Today, follow-up sequences and deal value

- **Today:** the page to open each morning. It lists leads that replied and are waiting for you (hottest first), follow-ups that are due, and leads who have not answered for 3 days. Each row has **WhatsApp** and **Email** buttons: the message opens already written for that person. A lead leaves the list once you answer it.
- **Follow-up sequences:** when writing a campaign, choose **If they don't reply**: one follow-up after 3 days, two (3 days, then 4 more) or three (3, 4, then 7). Each follow-up is written by the AI to fit what was already said, goes only to people who have not replied, and is counted from your last message to them on any channel (a WhatsApp you sent by hand counts). Replies, opt-outs and the usual daily limits and sending hours apply, and cancelling the first campaign cancels its follow-ups. Follow-ups need a free AI key.
- **Deal value:** on each lead, fill in **What you are offering** and **Deal value (KES)**. Overview shows the total won and the total still open (interested or meeting).

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
