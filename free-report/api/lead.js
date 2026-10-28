// Vercel serverless function: receives the free-report page's "look at my records" form and emails the details to YOU.
// Your CRM reads that email (it already reads your inbox) and turns it into a lead. Nothing is stored on Vercel.
//
// Environment variables (Vercel > Project > Settings > Environment Variables):
//   SMTP_HOST, SMTP_PORT   the mail server of a SENDING account, e.g. smtp.mail.yahoo.com and 587
//   SMTP_USER, SMTP_PASS   that account's address and APP password (use a spare account, not your main one)
//   NOTIFY_TO              the address your CRM reads (your main inbox)
//   LEAD_TOKEN             a secret of 16+ characters; paste the same one into the CRM (Grow > Content > Lead capture)
const nodemailer = require('nodemailer');

const hits = new Map();   // very small per-instance throttle: 5 submissions per 10 minutes per address
const one = (v, n) => String(v == null ? '' : v).replace(/[\r\n\t]+/g, ' ').trim().slice(0, n);   // one line only: no header or field injection

let transportOverride = null;
const transport = () => transportOverride || nodemailer.createTransport({
  host: process.env.SMTP_HOST, port: Number(process.env.SMTP_PORT || 587), secure: Number(process.env.SMTP_PORT) === 465,
  auth: { user: process.env.SMTP_USER, pass: process.env.SMTP_PASS }, connectionTimeout: 10000, socketTimeout: 15000
});

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
  const ip = one((req.headers['x-forwarded-for'] || '').split(',')[0] || 'x', 60), now = Date.now();
  const recent = (hits.get(ip) || []).filter((t) => now - t < 600000);
  if (recent.length >= 5) return res.status(429).json({ ok: false, error: 'Too many requests. Please try again later.' });
  hits.set(ip, [...recent, now]);
  const lines = [
    'Name: ' + name, 'Business: ' + business, 'Phone: ' + (phoneOk ? phone : ''), 'Email: ' + (emailOk ? email : ''),
    'Note: ' + one(b.note, 600), 'Summary: ' + one(b.summary, 300), 'Consent: yes', 'Token: ' + env.LEAD_TOKEN
  ];
  try {
    await transport().sendMail({ from: `"Health Check leads" <${env.SMTP_USER}>`, to: env.NOTIFY_TO, subject: 'Health Check lead: ' + (business || name), text: lines.join('\n') });
  } catch (e) {
    return res.status(502).json({ ok: false, error: 'We could not send your details just now. Please use the WhatsApp or email button instead.' });
  }
  return res.status(200).json({ ok: true });
}
module.exports = handler;
module.exports._useTransport = (t) => { transportOverride = t; };
module.exports._reset = () => hits.clear();
