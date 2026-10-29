# Free Business Health Check

A visitor uploads a sales sheet (CSV or Excel) and gets a report: best sellers, products that stopped selling, cash tied up in
slow stock, best months and days, and what sells together. The sales file is read inside the visitor's own browser and is never
uploaded. If they choose to leave their details at the end, those reach you as a lead in your CRM.

Contents of this folder
- `index.html`        the page, built in your site's own look (put it at `free-report/index.html` in your site; it uses your site's `assets/` folder)
- `engine.js`         reads and cleans any sales file and proves every number is accounted for (put it next to `index.html`)
- `vendor/`           the Excel reader, next to `index.html` as `free-report/vendor/`
- `api/lead.js`       the small free Vercel function that receives the "leave your details" form (copy to `api/lead.js` at the SITE ROOT)
- `api/lead-check.js` a self-check for that function (copy next to `lead.js`); the CRM's "Check lead capture" button calls it
- `api/lead-retry.js` re-sends saved leads (optional, see the safety net)
- `package.json`      the one dependency the function needs (merge into your site's package.json, or copy it if you have none)

## Step 1: the page (5 minutes)
1. Open `index.html`, find the `CONFIG` block near the bottom, and fill in your WhatsApp number (country code, no plus, e.g. `2547XXXXXXXX`)
   and your email.
2. In your site's project, create a `free-report` folder and put `index.html` inside. Deploy as you normally do.
   The page is then at `your-site/free-report/`.
3. In the CRM, open Grow > Content and save that address under "Your free-report link". It is then added to your post drafts and
   partner pitches, and your assistant can offer it.

## Step 2: automatic lead capture (optional, free, about 15 minutes)
This makes the "Want me to look at your full records?" form on the page work. If you skip it, set `leadForm: false` in `CONFIG`
(or just leave it: visitors can still use your WhatsApp and email buttons, and the form tells them so if the function is missing).
1. Copy `api/lead.js` to an `api` folder at the root of your site project, and add the `nodemailer` dependency from `package.json`.
2. In the CRM, open Grow > Content > "Lead capture", press Generate, press Save secret, and copy the value.
3. In Vercel: your project > Settings > Environment Variables, add these (then redeploy):
   - `LEAD_TOKEN`   the secret you just copied
   - `NOTIFY_TO`    the email address your CRM reads (your main inbox)
   - `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`   a SPARE sending account, for example one of your extra Yahoo or Gmail
     accounts, with its app password. Do not use your main account: its password would be stored on Vercel.
4. Try it: use the page with sample data, leave your own details, and within a few minutes (the next time the CRM checks your inbox)
   a lead appears with the source "free report".

How it works: the function emails you the details, signed with the secret. The CRM only accepts such an email if the secret
matches, only if the visitor ticked the consent box, and ignores everything else.

## Notes
- Excel (.xlsx) files open through a small public library loaded from cdnjs. Offline visitors are asked to save as CSV.
- The sample button uses invented data and says so.
- Everything shown is an estimate from the visitor's file. The page says this and promises no results.
- Protection against abuse: a hidden trap field, a minimum time on the form, and a limit of 5 submissions per 10 minutes per visitor.

## Optional safety net: never lose a lead (added)
In Vercel, open your project > Storage and connect a free Upstash Redis (or Vercel KV) store. Vercel adds its variables to the project by itself.
Then every lead is saved before the email is sent. If the email fails, the visitor still sees "Thank you" and the lead waits safely;
it is sent automatically on the next submission, or on demand by opening `/api/lead-retry` with the header `Authorization: Bearer <your LEAD_TOKEN>`
(a free cron service can call it hourly). Saved leads are deleted after 90 days. Without a store, everything works exactly as before.
Copy both `api/lead.js` and `api/lead-retry.js` to the `api` folder at your site root.

## Suggested next step (added)
The report page now suggests the service that fits what the file showed, and adds it to the lead summary so you can open your reply with it.

## Messy files (added)
Whatever is uploaded is cleaned in the visitor's browser and every row is accounted for: title lines above the headings, blank rows, repeated
headings, total and subtotal lines (shown, never added twice), returns and credits (negative amounts), KES / USD / "1.200,50" / "(500)" amounts,
unreadable amounts (listed, never dropped), day-first or month-first dates (decided from the file), Excel date numbers, monthly sheets (put together),
months across the page, Swahili headings, and UTF-8 / UTF-16 / Windows-1252 text. The report shows a "Your numbers add up" card:
sales + returns + total lines = everything read from the file. A cleaned copy of the file can be downloaded, listing what was done with each row.
Product names typed in several ways are counted together and the report says so; possible double entries are flagged, never removed.

## If the form says "We could not send your details just now" (added)
1. In the CRM press Grow > Content > Lead capture > "Check lead capture". It asks your website what is set up, in plain words.
   "Check and send a test email" also proves delivery (the test email is ignored by the CRM, it never becomes a lead).
2. The usual causes: a missing variable in Vercel, or the mail provider refusing the login (use an APP password for `SMTP_USER`).
   After changing variables in Vercel you must redeploy.
3. `vercel.json` gives the functions 30 seconds (the free default of 10 seconds is too short for a slow mail server).
4. The real reason is written to Vercel > your project > Logs (search "[lead]"). No password is ever logged.
5. While it is broken the visitor keeps what they typed and gets a one-tap "send these details on WhatsApp" link.

## Sales and expenses together (added)
The visitor can upload sales and expenses in two sheets (or two files' worth of sheets in one workbook), or in ONE sheet: with a Type column
(Sale / Expense) or with "Money in" and "Money out" columns. Which sheet is which is decided from its name and headings, and the page shows
the choice with a drop-down to change it, so the default just works and a wrong guess is one click to fix. Expenses get the same row-by-row accounting
as sales (their own "Your expenses add up" card and cleaned download), negative bank-style expenses are turned round, and the profit is worked out
only over the full months that both sales and expenses cover, so a partial month never distorts it. If the expenses cannot be read, the sales report
is still shown, with a note. A file with amounts but no product column is still read (as one line of business) instead of being refused.
