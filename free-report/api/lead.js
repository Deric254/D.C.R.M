// Vercel serverless function: receives the free-report page's "look at my records" form and emails the details to YOU.
// Your CRM reads that email (it already reads your inbox) and turns it into a lead.
//
// Environment variables (Vercel > Project > Settings > Environment Variables):
//   SMTP_HOST, SMTP_PORT   the mail server of a SENDING account, e.g. smtp.mail.yahoo.com and 587
//   SMTP_USER, SMTP_PASS   that account's address and APP password (use a spare account, not your main one)
//   NOTIFY_TO              the address your CRM reads (your main inbox)
//   LEAD_TOKEN             a secret of 16+ characters; paste the same one into the CRM (Grow > Content > Lead capture)
//
// OPTIONAL safety net (recommended): connect a free Upstash Redis / Vercel KV store to the project. When these are present
//   UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN   (or KV_REST_API_URL, KV_REST_API_TOKEN, which Vercel adds for you)
// every lead is saved BEFORE the email is sent, so a mail outage can no longer lose it, and the rate limit survives cold starts.
// Saved leads that could not be emailed are retried automatically on the next submission, and on demand through /api/lead-retry.
// Without these variables nothing changes: the function works exactly as before, with nothing stored.
const nodemailer = require('nodemailer');
const crypto = require('crypto');

const hits = new Map();   // fallback throttle when no store is connected: 5 submissions per 10 minutes per address
const one = (v, n) => String(v == null ? '' : v).replace(/[\r\n\t]+/g, ' ').trim().slice(0, n);   // one line only: no header or field injection

let transportOverride = null, storeOverride;
const transport = () => transportOverride || nodemailer.createTransport({
  host: process.env.SMTP_HOST, port: Number(process.env.SMTP_PORT || 587), secure: Number(process.env.SMTP_PORT) === 465,
  auth: { user: process.env.SMTP_USER, pass: process.env.SMTP_PASS }, connectionTimeout: 10000, socketTimeout: 15000
});

/* ---------- optional store (Upstash Redis / Vercel KV over HTTPS, no extra dependency) ---------- */
const KEEP = 60 * 60 * 24 * 90;   // saved leads are deleted after 90 days
function redisStore(url, token) {
  const run = async (cmds) => {
    const r = await fetch(url.replace(/\/$/, '') + '/pipeline', { method: 'POST', signal: AbortSignal.timeout(3000),
      headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' }, body: JSON.stringify(cmds) });
    if (!r.ok) throw new Error('store ' + r.status);
    const out = await r.json();
    if (out.some((x) => x && x.error)) throw new Error('store command failed');
    return out.map((x) => x.result);
  };
  return {
    async hit(key, seconds) { const [n] = await run([['INCR', key], ['EXPIRE', key, String(seconds), 'NX']]); return Number(n); },
    async save(rec) { await run([['SET', 'lead:' + rec.id, JSON.stringify(rec), 'EX', String(KEEP)], ['SADD', 'leads:pending', rec.id]]); },
    async done(id) { await run([['SREM', 'leads:pending', id]]); },
    async pending(max) {
      const [ids] = await run([['SMEMBERS', 'leads:pending']]);
      const list = (ids || []).slice(0, max);
      if (!list.length) return [];
      const got = await run(list.map((i) => ['GET', 'lead:' + i]));
      const recs = [], gone = [];
      got.forEach((g, i) => { if (g) { try { recs.push(JSON.parse(g)); } catch (e) { gone.push(list[i]); } } else gone.push(list[i]); });   // expired records are dropped
      if (gone.length) await run(gone.map((i) => ['SREM', 'leads:pending', i]));
      return recs;
    }
  };
}
function getStore() {
  if (storeOverride !== undefined) return storeOverride;
  const e = process.env, url = e.UPSTASH_REDIS_REST_URL || e.KV_REST_API_URL, token = e.UPSTASH_REDIS_REST_TOKEN || e.KV_REST_API_TOKEN;
  return url && token ? redisStore(url, token) : null;
}

const mailFor = (rec) => ({
  from: `"Health Check leads" <${process.env.SMTP_USER}>`, to: process.env.NOTIFY_TO,
  messageId: `<${rec.id}@free-report.dericbi>`,   // the same id on every retry, so the CRM never makes a second lead from a re-sent email
  subject: rec.subject, text: rec.text
});

// try to deliver leads that were saved but not emailed; returns how many were sent
async function flushPending(store, skipId, max) {
  if (!store) return 0;
  let sent = 0;
  try {
    for (const rec of await store.pending(max || 3)) {
      if (rec.id === skipId) continue;
      try { await transport().sendMail(mailFor(rec)); await store.done(rec.id); sent++; } catch (e) { break; }   // mail is down: stop, try again later
    }
  } catch (e) { /* the store is unreachable: nothing to do now */ }
  return sent;
}

async function handler(req, res) {
  res.setHeader('Cache-Control', 'no-store');
  if (req.method !== 'POST') return res.status(405).json({ ok: false, error: 'POST only' });
  const env = process.env;
  if (!env.NOTIFY_TO || !env.LEAD_TOKEN || (!transportOverride && (!env.SMTP_HOST || !env.SMTP_USER || !env.SMTP_PASS)))
    return res.status(500).json({ ok: false, error: 'The form is not set up yet.' });
  let b = req.body;
  try { if (typeof b === 'string') b = JSON.parse(b); } catch (e) { b = null; }
  if (!b || typeof b !== 'object') return res.status(400).json({ ok: false, error: 'Invalid request.' });
  if (b.website) return res.status(200).json({ ok: true });                          // hidden field: only bots fill it in; pretend it worked
  if (Number(b.elapsed) < 3000) return res.status(400).json({ ok: false, error: 'Please try again in a moment.' });
  if (b.consent !== true) return res.status(400).json({ ok: false, error: 'Please tick the box to say we may contact you.' });
  const name = one(b.name, 80), business = one(b.business, 120), phone = one(b.phone, 30), email = one(b.email, 120).toLowerCase();
  if (!(name || business)) return res.status(400).json({ ok: false, error: 'Please tell us your name or your business.' });
  const phoneOk = /^[+0-9][0-9 ()\-]{6,}$/.test(phone), emailOk = /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email);
  if (!phoneOk && !emailOk) return res.status(400).json({ ok: false, error: 'Please give a phone number or an email so we can reply.' });

  // Vercel sets these itself from the real connection, so a visitor cannot choose them
  const h = req.headers || {};
  const ip = one(h['x-vercel-forwarded-for'] || h['x-real-ip'] || (h['x-forwarded-for'] || '').split(',')[0] || 'x', 60), now = Date.now();
  const store = getStore();
  let limited = null;
  if (store) { try { limited = (await store.hit('rl:' + ip, 600)) > 5; } catch (e) { limited = null; } }   // store trouble: fall back below
  if (limited === null) {
    const recent = (hits.get(ip) || []).filter((t) => now - t < 600000);
    limited = recent.length >= 5;
    if (!limited) hits.set(ip, [...recent, now]);
  }
  if (limited) return res.status(429).json({ ok: false, error: 'Too many requests. Please try again later.' });

  const lines = [
    'Name: ' + name, 'Business: ' + business, 'Phone: ' + (phoneOk ? phone : ''), 'Email: ' + (emailOk ? email : ''),
    'Note: ' + one(b.note, 600), 'Summary: ' + one(b.summary, 300), 'Source: ' + one(b.source, 30).toLowerCase().replace(/[^a-z0-9_-]/g, ''), 'Consent: yes', 'Token: ' + env.LEAD_TOKEN
  ];
  const rec = { id: crypto.randomUUID(), at: new Date().toISOString(), subject: 'Health Check lead: ' + (business || name), text: lines.join('\n') };

  let saved = false;
  if (store) { try { await store.save(rec); saved = true; } catch (e) { saved = false; } }   // saving is a bonus; if it fails we carry on exactly as before

  try {
    await transport().sendMail(mailFor(rec));
  } catch (e) {
    if (saved) return res.status(200).json({ ok: true });   // the lead is safely stored and will be sent on the next retry
    return res.status(502).json({ ok: false, error: 'We could not send your details just now. Please use the WhatsApp or email button instead.' });
  }
  if (saved) { try { await store.done(rec.id); } catch (e) { /* it will simply be re-sent once; the CRM ignores the duplicate */ } }
  await flushPending(store, rec.id, 2);   // mail works right now, so also clear any earlier leads that were waiting
  return res.status(200).json({ ok: true });
}
module.exports = handler;
module.exports._useTransport = (t) => { transportOverride = t; };
module.exports._useStore = (s) => { storeOverride = s; };
module.exports._reset = () => hits.clear();
module.exports.flushPending = flushPending;
module.exports.getStore = getStore;
