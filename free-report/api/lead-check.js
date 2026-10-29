// Self-check for the lead form. Open it from your CRM (Grow > Content > Lead capture > Test) or call it by hand:
//   POST /api/lead-check      Authorization: Bearer <your LEAD_TOKEN>      optional body {"send": true}
// It tells you, without revealing any secret, which settings are present, whether the mail server accepts the login,
// and (with send:true) sends one clearly marked test email that your CRM ignores instead of turning into a lead.
const crypto = require('crypto');
const lead = require('./lead.js');

module.exports = async function (req, res) {
  res.setHeader('Cache-Control', 'no-store');
  const env = process.env, token = env.LEAD_TOKEN || '';
  const got = String((req.headers || {}).authorization || '').replace(/^Bearer\s+/i, '');
  const a = crypto.createHash('sha256').update(got).digest(), b = crypto.createHash('sha256').update(token).digest();
  if (!token || !crypto.timingSafeEqual(a, b)) return res.status(401).json({ ok: false, error: 'Wrong or missing token.' });
  const names = ['SMTP_HOST', 'SMTP_PORT', 'SMTP_USER', 'SMTP_PASS', 'NOTIFY_TO', 'LEAD_TOKEN'];
  const settings = {}; names.forEach((k) => { settings[k] = k === 'SMTP_PORT' ? (env[k] ? 'set (' + env[k] + ')' : 'not set (587 is used)') : (env[k] ? 'set' : 'MISSING'); });
  const missing = names.filter((k) => k !== 'SMTP_PORT' && !env[k]);
  const out = { ok: false, settings, missing, tokenLength: token.length, store: !!lead.getStore(), mailLogin: null, test: null, advice: [] };
  if (token.length < 16) out.advice.push('LEAD_TOKEN should be at least 16 characters.');
  if (!lead.getStore()) out.advice.push('No Upstash/KV store is connected. Without it a lead is lost if the mail server is down for a moment. Connecting a free one is recommended.');
  if (missing.length) { out.advice.unshift('Add the missing settings in Vercel > Project > Settings > Environment Variables, then redeploy: ' + missing.join(', ') + '.'); return res.status(200).json(out); }
  const t = lead.transport();
  try { await t.verify(); out.mailLogin = 'ok'; }
  catch (e) {
    out.mailLogin = 'FAILED: ' + lead.why(e);
    out.advice.push(/535|auth|credential|password|username/i.test(out.mailLogin) ? 'The mail server refused the login. Use an APP password (not the normal password) for SMTP_USER, and check SMTP_HOST/SMTP_PORT match that provider.' : /ETIMEDOUT|ECONNREFUSED|ENOTFOUND|ESOCKET|timeout/i.test(out.mailLogin) ? 'The mail server could not be reached from Vercel. Check SMTP_HOST and SMTP_PORT (587, or 465 for SSL).' : 'The mail login failed. Read the message above.');
    return res.status(200).json(out);
  }
  let body = req.body; try { if (typeof body === 'string') body = JSON.parse(body); } catch (e) { body = {}; }
  if (body && body.send === true) {
    try {
      const id = crypto.randomUUID();
      await t.sendMail({ from: `"Health Check leads" <${env.SMTP_USER}>`, to: env.NOTIFY_TO, messageId: `<${id}@free-report.dericbi>`, subject: 'Health Check lead TEST: ignore', text: 'This is a test from /api/lead-check. Your CRM treats it as a test and does not create a lead.\nTest id: ' + id });
      out.test = 'sent to ' + env.NOTIFY_TO.replace(/^(.).*(@.*)$/, '$1***$2');
    } catch (e) { out.test = 'FAILED: ' + lead.why(e); out.advice.push('The login works but sending failed. Read the message above.'); return res.status(200).json(out); }
  }
  out.ok = true;
  return res.status(200).json(out);
};
