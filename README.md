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

Settings > AI writing help takes free keys from Google Gemini, Groq, NVIDIA, OpenRouter, Mistral, or any OpenAI-compatible server (such as Ollama). Keys are tried in order, so when one hits its free limit the next one answers. **Write with AI** appears when you write a campaign and when you message one lead (it replies to their latest message). Free-tier model names get retired every few months. The app tries a list of current models for each provider and, if they are all gone, asks the provider which models it offers today and uses one that works (**Test the keys** shows which). You can still type a model name of your own. Settings also connects Gmail in one step and has presets for Outlook/Zoho/Yahoo, plus step-by-step guides for getting the passwords and keys.

## WhatsApp, email and text from one lead

Open a lead and press **WhatsApp**, **Email** or **Text**. The AI writes the message straight away from the lead's business, town and type of business, your notes, and the whole conversation so far: a first hello that introduces you and your offer, an answer to something they said, or a follow-up that refers to your earlier message when they have not replied (a different angle each time, and a gracious last note after three). Edit it or press **Write with AI** for another version.

- **WhatsApp is free:** once WhatsApp is connected (Settings > WhatsApp) the message is sent for you and recorded automatically. Until then **Open WhatsApp** logs the message and opens the chat with the text ready; you press send. Log their answer with **Log a reply > WhatsApp**.
- **Everything is tracked:** each reply is matched to the message of ours it answers ("In answer to our WhatsApp of ..."), a lead you are still waiting on shows **Awaiting reply**, and Overview shows how many leads answered on each channel.
- **Your identity:** Settings > Sender name and Settings > AI writing help > *Who you are and what you sell* decide how the AI introduces you (default: Deric Marangu, Business Intelligence specialist at DericBI, with the offer and the "You only pay when you see value" line from the owner's own message). Settings > Business name sets the brand. The AI follows the owner's own message as its plan: a greeting, "I came across you online", who I am, two or three concrete things to look into for that kind of business, how it is delivered, and a no-pressure close. Emails stay near 100 words and WhatsApp messages under 300 characters; a draft that comes out longer is shortened, and if it still is, the ready-made version of the same message is sent instead. Edit the pitch to change any of it.

## Accounts and licences

Nothing in the app opens until someone has signed in. The first account on the owner's computer is **Deric Marangu**, the owner (mother) account.

- **Passwords** must be strong: 12+ characters with a capital, a number and a symbol, no common passwords, no part of the name, no runs like `1234`. *Suggest a strong password* makes one. Passwords and security answers are stored only as salted scrypt hashes. Five wrong passwords lock the account for a while, and the wait grows each time.
- **Security questions:** three, all needed to reset a forgotten password (and, for the owner, the owner key as well). Changing a password signs out every other place.
- **Other people** are added by the owner in *Settings > People on this computer*. They get a temporary password and must choose their own password and questions before they can use anything. Only the owner can add, switch off, reset or remove people.
- **Another computer needs the owner's signature.** A new computer shows an *activation code*. The owner enters that code in *Settings > Let another computer use the app*, adds the person's name, pastes the **owner key**, and gets a licence to send back. The licence is signed (Ed25519) and works on that computer only, for the days chosen. Every copy of the app carries only the owner's *public* key, so it can check a licence but never make one.

**One-time setup (do this before you build the installer):**

1. Run `python scripts/make-owner-key.py`. It writes `dericbi-owner.key` (keep it secret and make a backup: it is never committed, and never saved by the app) and prints a public key.
2. Paste that public key into `backend/owner_key.py` and commit that file.
3. The release build now fails if the public key is missing, so an unprotected installer can't go out by mistake.
4. On your own computer, open the app and create the owner account with your name and the owner key.

**What this does and doesn't do:** it stops anyone using a copy without your licence, and stops people using each other's accounts. The app is not hosted, so a licence can't be taken back from a computer that is offline: it simply ends on its date, so issue short ones and renew them. A determined person who edits the program files could remove the check; licences and passwords keep honest users honest, they are not copy-protection against that. The data file itself is not encrypted, so turn on Windows BitLocker or a Windows password for the computer.

## Email, WhatsApp and texts (Outreach)

Press **New campaign** (or tick leads on the Leads page and press **Reach out**): 1) choose how to reach them: WhatsApp, Email, Text, or **Best way for each lead** (each lead gets ONE message on the first way that works for them, in the order set in Settings; if a way fails, such as a number that isn't on WhatsApp or an email that bounces, that same message moves to the next way, so nobody is ever reached twice), 2) tick the leads (filter by sector, town or name; untick anyone you want to leave out), 3) choose the message, 4) choose follow-ups, then **Start campaign**.

- **If one way does not work, another is tried:** with *Best way for each lead* a lead who is not on WhatsApp, whose email is refused, or whose text could not be delivered has their ONE message moved to their next way. A campaign on a single way does the same when you tick *reach them another way*. A message that may already have been delivered is never moved.
- **Email (Gmail):** Settings > Email > *Connect Gmail*: your Gmail address and an App Password (the page shows how to get one). It sets up sending and reading replies, then tests both and shows each step with a tick or cross, so a failure says exactly what to change.
- **Texts (your Android phone):** install the free open-source *SMS Gateway for Android* app on the phone, switch on its Local server, and put its address, username and password in Settings > Text messages. The phone and the PC must be on the same Wi-Fi/router or hotspot. Texts are sent by your SIM (your normal SMS bundle applies). Android only allows about 30 texts per 30 minutes per app, so the app waits at least 30 seconds between texts. A text is always one SMS of 160 characters or less, opt-out line included; a long business name is shortened to fit.
- **WhatsApp:** Settings > WhatsApp > *Connect WhatsApp* opens WhatsApp Web once for you to scan the code. After that campaigns send by themselves, one at a time with random pauses, and a message is recorded as sent only after WhatsApp shows it as sent. A number that isn't on WhatsApp is skipped and remembered. If WhatsApp logs out, or three messages in a row fail, every WhatsApp campaign is paused. WhatsApp can ban numbers that message many people who never chatted with them, so use a number you can afford to lose and keep the daily limit low. Until WhatsApp is connected, **Send by hand** gives you one chat at a time.
- **One browser, never a second message:** a WhatsApp run uses ONE browser window from the first message to the last, and stays in it while a retry waits. If the browser cannot start, or keeps closing, the campaign is paused with the reason instead of opening a window every minute. Before each message the worker looks at the chat itself: if that exact message is already the last thing you sent there it is recorded and NOT sent again, and a follow-up is not sent to someone who has written back since (a person or their business's auto-reply). Once Send has been pressed, a problem is never treated as "try later": the message is marked failed with a warning, and **Retry failed** leaves such messages alone (message that lead by hand after checking the chat). No lead is ever sent two campaign messages within an hour, whatever the cause; a reply to someone who wrote back, or a message you send by hand, is never held up.
- **No window, and replies recorded by themselves:** WhatsApp runs in a hidden browser (Settings > WhatsApp > *Hide the WhatsApp window*; switch it off if WhatsApp will not load hidden). Connecting still shows a window once, for the code. Every 10 minutes (Settings, or **Check for replies now**) the same hidden session opens the chats of leads you WhatsApped in the last 14 days and records what they wrote back in the campaign that messaged them; each WhatsApp message is stored once, so nothing doubles. A queued follow-up is dropped if the lead has replied meanwhile.
- **Never a second first message:** a message that was sent but not confirmed ("may or may not have been delivered") counts as sent for every new campaign and hand send. If your WhatsApp chat with a lead already contains messages from you (earlier, or typed on your phone), a campaign's first message is skipped and the lead marked contacted. Skipped leads show when, how and in which campaign they were already messaged.
- **Ready-made messages:** the default message mode writes each lead a message for their kind of business (pharmacy, agrovet, hardware, salon, restaurant, school, garage, shop): a real pain point, a curiosity hook, one easy next step, and your website, WhatsApp and email from Settings > Your business. No AI key needed.
- **One message at a time:** a lead who has a message is left out of new campaigns, and cannot be messaged by hand either, until the follow-up wait (Settings, default 3 days) has passed with no reply, or they reply. **Follow up** on a finished campaign messages only the people who haven't answered, on the same channel or another.

## AI sales assistant (Assistant page)

Reads what leads write back on **WhatsApp, text and email**, finds their pain and every need they mention, and moves each chat to ONE next step (a short call, a visit, a clear yes), courteously and without pressure. It needs a free AI key; each channel needs to be connected (the Assistant page shows which are).

- **Conversation mode switch (top of the page):** ON = it watches for messages from leads and answers, in the way you choose (*Suggests, I send* or *Sends the safe ones*); OFF = it stays completely silent and you answer yourself. A pill at the top and a badge on the Assistant menu show when it is on and how many replies wait for you. *Assistant off for this lead* switches it off for one person.
- **Deal desk (Assistant > Setup):** the only prices it will ever quote, always exactly: the report on a business's own existing data (never more than the maximum you set, KES 2,000), and the ready-to-use system for a business with no system, in two lanes for them to choose: KES 20,000 once, or KES 1,000 to start and KES 200 per month. A reply that prices the report above your maximum, quotes any other amount, says "around", "maybe" or gives a range is rejected. Discount requests always come to you. A business with records is offered the report first (the smallest step); one with none, the system; one with both needs, the report first and the system after.
- **Memory across days:** it is told how long ago each message was and what it has learned about this lead (their pain, every need it noticed, the best-fit offer, anything it promised, the stage). When someone replies tomorrow or in a week, it welcomes them back and continues where you left off. Pain, needs and offer are also written onto the lead (its Offer and Deal value, when you have not set them).
- **Chats that start elsewhere:** with conversation mode on, the hidden WhatsApp session stays open and looks at chats WhatsApp shows as unread: a stranger who writes to you becomes a lead (source "inbound whatsapp"), and someone you chatted with from your phone who follows up later is matched by number (or, for a saved contact, by exact name). What you wrote from your phone is kept as context. A stranger who emails you becomes a lead the same way (mailing lists, no-reply and automatic mail never do). Note: opening an unread chat marks it read on your phone.
- **Courtesy and psychology, honestly used:** it thanks them, gives something useful first, asks about THEIR numbers, asks for the smallest next step, offers clear choices and says plainly it is their call. No urgency, scarcity, flattery, guilt or fear; a reply with pressure words, more than one question, a blank, or a claim that something is free (when your offer does not say so) is rejected.
- **Two checks on every reply:** the code's rules, then a second AI pass that reviews the draft against your offer (true? answers them? courteous? unambiguous? one next step? nothing repeated?). A rejected reply is rewritten (up to twice); if the reviewer cannot run, or keeps saying no, nothing is sent by itself.
- **Three modes under the switch:** *Suggest*; *Auto for low-risk turns* (confident, approved by both checks, after a human-like pause, at most 3 in a row and 30 an hour, nothing between 21:30 and 07:00: it waits for the morning). **Auto only works while the Test pack has passed in full on your current offer**; change the offer or the prompt and it pauses until you run the pack again. After three AI failures in a row, automatic sending pauses for ten minutes.
- **Always yours, whatever the AI says:** discounts, payment details, contracts and legal talk, anger, "where did you get my number", a request for a person, a booking, an attempt to re-programme it, and any reply that fails the checks. Opt-outs, do-not-contact leads and automatic replies are never answered.
- **You stay in charge:** if you write to a lead yourself the assistant steps aside for 20 minutes and never answers what you already answered. A chat that went quiet after the assistant's last message gets ONE gentle check-in idea after two days (08:00 to 19:00), always as a draft for you.
- **Honest:** its first reply says it is Deric's AI assistant (or only when asked, your choice); asked "are you a bot?" it always says so. It never claims to be human.
- **Each channel in its own shape:** WhatsApp short; text one SMS (160 characters, no opt-out tag inside a conversation); email a proper reply with a "Re:" subject, greeting and sign-off, with the quoted text of the thread removed. Text replies from leads cannot arrive by themselves (the phone cannot reach this computer): log them with *Log a reply* and the assistant takes it from there.
- **Try it:** *Try it* runs any made-up reply (any channel, "they replied 3 days later", with what it already knows) through the real pipeline and shows the reply, the needs it found, the offer, the stage, its confidence, and exactly what would happen (nothing is sent or saved). *Test pack* runs 26 typical and tricky situations through your real AI and checks each by rule.
- **Measure it:** the line at the top shows drafts waiting, sent by itself, sent by you, how often you changed its words, who kept talking, and meetings.
- Answers inside a conversation never use up the daily limits for campaign messages. The assistant runs in its own thread in the app, so a slow AI never holds up the WhatsApp browser.

## Today, follow-up sequences and deal value

- **Today:** the page to open each morning. It lists leads that replied and are waiting for you (hottest first), follow-ups that are due, and leads who have not answered for 3 days. Each row has **WhatsApp** and **Email** buttons: the message opens already written for that person. A lead leaves the list once you answer it.
- **Follow-up sequences:** when writing a campaign, choose **If they don't reply**: one follow-up after 3 days, two (3 days, then 4 more) or three (3, 4, then 7). Each follow-up (written by the AI when a key is set, otherwise ready-made: a gentle nudge, something useful, a polite last note) goes only to people who have not replied, and is counted from your last message to them on any channel (a WhatsApp you sent by hand counts). Replies, opt-outs and the usual daily limits and sending hours apply, and cancelling the first campaign cancels its follow-ups. Follow-ups need a free AI key.
- **Deal value:** on each lead, fill in **What you are offering** and **Deal value (KES)**. Overview shows the total won and the total still open (interested or meeting).

## AI campaigns (Outreach)

On **Outreach**, tick **Let the AI write each message for each person**. The message box becomes a brief (the goal, the offer, what you want them to do) and the AI writes every lead's own message just before it is sent, from everything you know about that lead: business, town, address, website, status, your notes and the conversation so far. Emails and texts go out through the same sender as any campaign, with the same daily limits, sending hours, opt-out line and do-not-contact rules.

- **Set up with AI** at the top of the form turns a sentence like "email the pharmacies in Meru about my billing software" into a filled-in campaign (channel, audience, brief). You check it, then press Start. Nothing is sent until you do.
- **Show example messages** drafts the first few so you can judge the writing before starting. They aren't kept; each real message is written fresh.
- **No repeats:** a lead who already has a message (any channel, any campaign, or by hand) is never picked for a new campaign, and a message that comes out nearly identical to another in the campaign is reworded once, then held back (shown as Failed; **Retry failed** writes it again).
- **Never half-written:** a message with a leftover `{blank}` or no email subject is not sent. If the AI is busy or out of free limit the message waits and retries; if no AI key is set the campaign pauses.
- **The log:** the exact text of every message is kept. Open a campaign's **Details** to read what each lead got, or open the lead to see it in their history.

## Backups, recovery and moving to another computer

**Settings > Backups and moving to another computer** (owner only). A backup is your whole business in one file: leads, messages, campaigns, history, settings and logo. It never holds sign-in accounts, security answers or this computer's licence, so restoring one cannot change who may sign in or copy a licence to another computer. Saved passwords and API keys are left out unless you tick the box when you make the backup (then keep that file private).

- **Automatic backups**: one a day (settable), the newest 7 kept, in the `backups` folder inside your data folder. Set a second folder (a USB stick or a synced cloud folder) to keep a copy that survives this computer.
- **Restore**: press Restore next to a backup. First a safety copy of what is there now is saved, then everything is replaced in one step: it all happens or none of it does. A backup that is damaged, was changed after it was made, or comes from a newer version of the app is refused before anything is touched.
- **Moving to another computer**: install the app there and sign in, then in Settings choose the backup file under "Bring a backup from another computer" and press Restore. Campaigns that were running come back **paused**, and messages caught mid-send come back as "check before retrying", so two computers never message the same people. Turn sending off on the old computer first. WhatsApp has to be connected again, and a customer's new computer needs its own licence.
- **A damaged data file**: if the data file can no longer be opened, the app sets it aside (it is kept, not deleted), starts a fresh one and restores the newest backup. The Backups panel says what happened. Accounts are not part of a backup, so they are made again.
- Measured at about a million leads (five million records in all): a backup takes about half a minute (219 MB), a restore about forty seconds, and the app stays responsive while they run.

## Speed on a slow computer

The app was measured on a deliberately starved computer (one processor core shared with a CPU-hungry program, and for the browser a further 6x slowdown) with a large database: 30,000 leads, 40 campaigns, 30,000 messages. Run the same measurements yourself:

- `python tests/perf_bench.py --leads 30000 --hog` times what every screen asks the server for, and the CPU used while the app is just left open.
- `python tests/perf_ui.py --data <folder made by perf_bench.seed> --throttle 6` times each screen in Chrome and how long the page freezes.

What changed, and what it did (before -> after): **Today 9.4 s -> 0.2 s** (it read every lead and counted per row; it now reads only the 40 shown), **Overview 3.1 s -> 0.4 s** (one pass over the leads instead of about 20), **campaign list** 11 scans per campaign -> one, **Leads page** no longer sorts every lead on each page view (0.2 ms instead of 50 ms), **best-way counts and previews** are one query instead of a lookup per lead, and **New campaign** draws 100 leads at a time instead of 1,000 (its freeze fell from 1.0 s to 0.2 s). Screens now stop refreshing while the window is hidden or minimised, slow down when nothing is running, and skip redrawing when nothing changed. The database is set up for a slow disk (write-ahead log, normal sync) and learns the shape of your data once.

The tests keep it that way: they check the new queries give exactly the old answers on randomised data, and that the work a screen does does not grow with the number of leads.

**Light mode** (Settings > Limits): each Chrome window costs about 0.7 GB, and the lead search and WhatsApp each open one. On a computer with 4 GB of memory or less, or two processor cores, light mode switches on by itself and runs one browser at a time (WhatsApp waits for a lead search and the reverse). You can force it on or off.

**Not changed, and the biggest remaining cost on a very slow computer:** the installer packs the engine as one file that includes Playwright, so every launch first unpacks well over 100 MB into a temporary folder, which on a slow disk with antivirus scanning can take many seconds before the app opens. The cure is to build the engine as a folder (`pyinstaller --onedir`) and ship that folder with the app, which also needs a change to how the Tauri shell finds and starts it (`src-tauri/src/main.rs` and `tauri.conf.json`). That can only be built and tested on Windows, so it was not changed blind. Until then, keeping the app open instead of closing and reopening it avoids the cost.

## Local use

Building the installer on your own PC needs Python, Node.js, Rust and the **Visual Studio Build Tools** (workload “Desktop development with C++”). GitHub builds it for you on every push, so you only need these to build locally.


- `dev.bat` runs the desktop app from source.
- `start-in-browser.bat` runs it in the browser (no updater).
- `build-installer.bat` builds an installer locally.
- `python tests/test_backend.py` runs the backend tests.

Data lives in `%APPDATA%\DericBI-CRM` and survives updates.
