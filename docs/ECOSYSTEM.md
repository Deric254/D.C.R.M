# The dericBI ecosystem: how your systems know about each other

Goal: your systems (CRM, website and Health Check, Business Reality Engine, and the business systems) work as one family without any of them
being written around another. No system's code names another system. Each one only (1) publishes a small **capability card** and (2) keeps
a list of addresses. A new system is a new address; a system that is off just shows "not reachable".

## The capability card (contract `dericbi.ecosystem/1`)
A small JSON file every system publishes. Only `contract`, `id` and `name` are required.

```json
{ "contract": "dericbi.ecosystem/1", "id": "bre", "name": "Business Reality Engine", "version": "1.0",
  "role": "what it is, in a few words",
  "offers": ["alerts", "forecast"], "accepts": ["events.webhook", "files.upload"], "health": "/api/health/" }
```
`offers` and `accepts` are plain strings. Any system may invent new ones; nobody needs a central list. Unknown fields are kept and ignored.

A card may also carry `"handoffs": {"licence.issue": "/master?issue_to={name}&ref={ref}"}`: a ready-made link (a plain path on that same system, `{name}` and `{ref}` filled in by the opener) that another system can show its owner. Anything that is not a plain path is dropped. Opening it sends nothing; it only fills a form that the owner still submits. The CRM shows it as a button on a won lead (`{ref}` is the lead's permanent reference, the same on any copy of the CRM database, kept on the licence only so alerts can be matched back).
A card may also list `"feeds": {"advisor.alerts": "/api/advisor/alerts"}`: a place another system may read from, under that system's own rules.
Cards hold no secrets and no business data.

## Where each card lives (the CRM tries these in order)
1. `/.well-known/dericbi.json`   2. `/dericbi.json`   3. `/api/ecosystem/card`
- Website: `.well-known/dericbi.json` and `dericbi.json` (static files).
- Business Reality Engine: `GET /api/ecosystem/card` (`app/api/ecosystem.py`, no database needed).
- CRM: `GET /api/ecosystem/card` (for display; it needs sign-in).

## In the CRM
Settings > Data and backups > **Your other systems**: add a name and address, press Add and check. It shows reachable or not, what the
system is, and what it offers and accepts. Owner only. Nothing is sent to those systems from the CRM.

## Rules for adding the next system
- Publish a card. Do not edit another system to know about yours.
- Capabilities are strings; add one when you add a feature.
- A system that is missing, down, or on a newer card version must never break another: show it in words and carry on.
- When data starts to flow (next step), it goes through each system's own existing door (for example BRE's per-connector webhook token) with
  a retry queue, so a down system never loses or blocks anything.

## Alerts from businesses back to the CRM (opt-in, narrow)
Many businesses use the Business Reality Engine; their alerts are theirs. So nothing flows by default:
1. A business only counts if its licence was issued from a CRM lead (the licence carries that lead's number).
2. Its own owner must switch on **Tell my DericBI advisor about serious problems** (Settings; off by default, can be turned off at any time, and says what is shared).
3. Only critical and high alerts that are still open, and only: lead number, business name, severity, the headline, the time. Never records, figures behind it, or explanations. A headline can name a branch or product and contain a percentage ("Westlands sales are down 15%"); the switch says so.
4. Read-only, behind `ADVISOR_FEED_KEY` set on the engine. If it is not set the feed does not exist (404). The same value goes in the CRM (Settings > Data and backups > Your other systems > Alert feed key), where it is a saved secret like the mail password.
5. The CRM asks every 5 minutes (it is a desktop app, so the engine cannot call it). Each alert becomes one follow-up on that lead, once: due today unless one is already due or overdue, and a later planned date that is brought forward is written in the lead's history. "Check alerts now" does it on demand and says in words what happened.

## Not built yet (on purpose)
1. Quiet clients (no data for days) coming back as follow-ups, and anything flowing the other way.
2. Shared fixture files so the Health Check page and BRE read messy files identically.
Each will be off by default and use the cards to decide whether it applies.
