// Re-sends saved leads whose email failed. Needs the optional store (see lead.js). Safe to call as often as you like.
// Call it with:  Authorization: Bearer <your LEAD_TOKEN>   (for example from a free cron service, or by hand)
const crypto = require('crypto');
const lead = require('./lead.js');

module.exports = async function (req, res) {
  res.setHeader('Cache-Control', 'no-store');
  const token = process.env.LEAD_TOKEN || '';
  const got = String((req.headers || {}).authorization || '').replace(/^Bearer\s+/i, '');
  const a = crypto.createHash('sha256').update(got).digest(), b = crypto.createHash('sha256').update(token).digest();
  if (!token || !crypto.timingSafeEqual(a, b)) return res.status(401).json({ ok: false });
  const store = lead.getStore();
  if (!store) return res.status(200).json({ ok: true, store: false, sent: 0 });
  const sent = await lead.flushPending(store, null, 20);
  return res.status(200).json({ ok: true, store: true, sent });
};
