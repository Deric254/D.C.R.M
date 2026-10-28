# Free Business Health Check

A visitor uploads a sales sheet (CSV or Excel) and gets a report: best sellers, products that stopped selling, cash tied up in
slow stock, best months and days, and what sells together. The sales file is read inside the visitor's own browser and is never
uploaded. If they choose to leave their details at the end, those reach you as a lead in your CRM.

Contents of this folder
- `index.html`        the page (put it at `free-report/index.html` in your site)
- `api/lead.js`       the small free Vercel function that receives the "leave your details" form (copy to `api/lead.js` at the SITE ROOT)
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
