// Runs the REAL app scripts (core.js, auth-ui.js, view-settings.js) in a simulated browser against a fake server, and uses the
// Backups panel the way a person would: every button, the file upload, the error cases, the notices and a staff account.
// It is run by hand, because it needs a browser simulator that the app itself does not use:
//     npm install --no-save jsdom        (once; Node 20 or newer)
//     node tests/ui_backups.test.js
const { JSDOM, VirtualConsole, requestInterceptor } = require('jsdom');
const fs = require('fs'), path = require('path');
const STATIC = process.argv[2] || path.join(__dirname, '..', 'backend', 'static');

const loader = requestInterceptor((request) => {   // the app's own files come straight from the project folder
  const f = path.join(STATIC, new URL(request.url).pathname);
  return new Response(fs.existsSync(f) ? fs.readFileSync(f) : '', { headers: { 'Content-Type': f.endsWith('.js') ? 'application/javascript' : 'text/plain' } });
});
const results = []; const check = (name, ok, extra) => { results.push(!!ok); console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok ? '' : '  ' + (extra || ''))); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const waitFor = async (fn, what, ms = 4000) => { const t = Date.now(); while (Date.now() - t < ms) { try { if (fn()) return true; } catch (_) {} await sleep(10); } throw new Error('timed out waiting for: ' + what); };

function makeServer(opts = {}) {
  const S = { calls: [], toasts: [], timers: [], downloads: [], confirms: [],
    items: opts.items || [
      { name: 'DericBI-backup-20261015-020000-auto.zip', bytes: 219e6, created: '2026-10-15T02:00:00', label: 'auto', counts: { leads: 996902, messages: 1079955, campaigns: 40 }, includes_secrets: false },
      { name: 'DericBI-backup-20261014-101500-manual.zip', bytes: 4.2e6, created: '2026-10-14T10:15:00', label: 'manual', counts: { leads: 2400, messages: 3100, campaigns: 3 }, includes_secrets: true },
      { name: 'DericBI-backup-20250101-000000-manual.zip', bytes: 12, created: '', label: 'damaged', counts: {}, error: 'That file is not a DericBI backup.' }],
    op: null, running: false, step: '', error: '', result: null, polls: 0, pending: null, notice: opts.notice || null, health: opts.health || { ok: true }, owner: opts.owner !== false,
    failNext: opts.failNext || '' };
  const json = (status, body) => ({ ok: status < 400, status, json: async () => body });
  S.fetch = async (url, o = {}) => {
    const u = new URL(url, 'http://127.0.0.1:8000'); const p = u.pathname + u.search; const m = (o.method || 'GET').toUpperCase();
    const body = typeof o.body === 'string' ? JSON.parse(o.body) : o.body; S.calls.push({ m, p, body });
    if (p.startsWith('/api/backups')) {
      if (!S.owner) return json(403, { detail: 'Only the owner account can do that.' });
      if (m === 'GET' && p === '/api/backups') return json(200, { items: S.items, dir: 'C:\\Users\\Deric\\AppData\\Roaming\\DericBI\\backups', status: {}, free_mb: 5123,
        auto: { on: true, hours: 24, keep: 7, external: opts.external || '', due: false }, health: S.health, notice: S.notice });
      if (m === 'GET' && p === '/api/backups/status') {
        if (S.running && ++S.polls >= 2) { S.running = false; S.step = 'Done'; if (S.pending) { S.pending(); S.pending = null; } }
        return json(200, { op: S.op, step: S.step, error: S.error, result: S.result, running: S.running }); }
      if (m === 'POST' && p === '/api/backups/create') { S.op = 'backup'; S.running = true; S.polls = 0; S.step = 'Copying your data'; S.error = ''; S.result = null;
        S.pending = () => { if (S.failNext) { S.error = S.failNext; return; } S.result = { name: 'DericBI-backup-20261016-120000-manual.zip', bytes: 5.5e6, counts: { leads: 10 } };
          S.items.unshift({ name: S.result.name, bytes: S.result.bytes, created: '2026-10-16T12:00:00', label: 'manual', counts: { leads: 10, messages: 20, campaigns: 1 } }); };
        return json(200, { started: true }); }
      if (m === 'POST' && p.endsWith('/restore')) { S.op = 'restore'; S.running = true; S.polls = 0; S.step = 'Replacing your data'; S.error = ''; S.result = null;
        S.pending = () => { if (S.failNext) { S.error = S.failNext; return; } S.result = { restored: { leads: 996902, messages: 1079955 }, paused_campaigns: 2, safety_copy: 'x-before-restore.zip' }; };
        return json(200, { started: true }); }
      if (m === 'DELETE') { S.items = S.items.filter((i) => '/api/backups/' + encodeURIComponent(i.name) !== p); return json(200, { ok: true }); }
      if (m === 'PUT' && p.startsWith('/api/backups/upload')) { const name = decodeURIComponent(u.searchParams.get('name'));
        S.items.unshift({ name: 'uploaded-' + name, bytes: o.body.size, created: '2026-10-16T12:30:00', label: 'manual', counts: { leads: 77, messages: 5, campaigns: 1 } }); return json(200, { name: 'uploaded-' + name }); }
      if (m === 'POST' && p === '/api/backups/dismiss-notice') { S.notice = null; return json(200, { ok: true }); }
    }
    if (p === '/api/settings' && m === 'GET') return json(200, { sender_name: 'Deric Marangu', brand_name: 'DericBI', backup_auto: true, backup_hours: 24, backup_keep: 7, backup_dir: '' });
    if (p === '/api/settings' && m === 'PUT') return json(200, body);
    if (p === '/api/health') return json(200, { ok: true, version: '1.2.3', data_dir: 'C:\\Users\\Deric\\AppData\\Roaming\\DericBI', machine: {} });
    if (p === '/api/status') return json(200, {});
    if (p.startsWith('/api/auth/')) return json(401, { detail: 'Please sign in.', auth: 'login' });
    return json(200, {});
  };
  return S;
}

async function boot(S) {
  const vc = new VirtualConsole();   // the simulated browser's own complaints are not what is being tested
  const html = fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8');
  const dom = new JSDOM(html, { url: 'http://127.0.0.1:8000/', runScripts: 'dangerously', resources: { interceptors: [loader] }, pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.fetch = (u, o) => S.fetch(u, o);
      const real = w.setTimeout.bind(w);
      w.setTimeout = (fn, ms, ...a) => { S.timers.push(ms); if (ms >= 2000) return 0; return real(fn, Math.min(ms || 0, 3), ...a); };
    } });
  await new Promise((r) => dom.window.addEventListener('load', r));
  await sleep(150);
  const w = dom.window; const App = w.App;
  App.confirm = async (msg) => { S.confirms.push(msg); return true; };
  App.download = (u) => S.downloads.push(u);
  new w.MutationObserver((ms) => ms.forEach((mm) => mm.addedNodes.forEach((n) => n.className && S.toasts.push({ text: n.textContent, bad: /bad/.test(n.className) })))).observe(w.document.querySelector('#toasts'), { childList: true });
  const host = w.document.createElement('div'); w.document.body.appendChild(host);
  await App.views.settings.render(host);
  await waitFor(() => host.querySelector('#bk-body') && !/Loading/.test(host.querySelector('#bk-body').textContent), 'the Backups panel to fill in');
  return { w, host, App, S };
}
const text = (host) => host.querySelector('#backups-panel').textContent.replace(/\s+/g, ' ');

(async () => {
  // 1. what the panel shows
  let S = makeServer({ external: 'E:\\USB\\DericBI' }); let { w, host } = await boot(S);
  const t = text(host);
  check('the panel is on the Settings screen with its explanation', /Backups and moving to another computer/.test(t) && /Sign-in accounts and this computer's licence are never in it/.test(t));
  check('it shows the folder, free space, automatic-backup status and the second folder', /DericBI.backups/.test(t) && /5,123 MB free/.test(t) && /automatic backup on, every 24 h, keeping 7/.test(t) && /second copy to E:\\USB\\DericBI/.test(t), t.slice(0, 300));
  check('each good backup shows its kind, size and what is in it', /Automatic · 2026-10-15 02:00 · 219 MB/.test(t) && /996,902 leads, 1,079,955 messages, 40 campaigns/.test(t) && /Made by you · 2026-10-14 10:15 · 4\.2 MB · includes passwords/.test(t), t);
  check('a damaged file is flagged and cannot be restored, only deleted', /That file is not a DericBI backup/.test(t) && host.querySelectorAll('[data-bk="restore"]').length === 2 && host.querySelectorAll('[data-bk="delete"]').length === 3);
  check('the automatic-backup options are in the settings form so Save keeps them', ['backup_auto', 'backup_hours', 'backup_keep', 'backup_dir'].every((n) => host.querySelector(`#sform [name="${n}"]`)));
  check('there is a way to bring in a backup from another computer, with the moving instructions', host.querySelector('#bk-file') && /come back paused, so two computers never message the same people/.test(t) && /WhatsApp has to be connected again/.test(t) && /needs its own licence/.test(t));

  // 2. Back up now (without and with passwords)
  host.querySelector('[data-bk="now"]').click();
  await waitFor(() => S.toasts.some((x) => /Backup saved \(5\.5 MB\)/.test(x.text)), 'the backup to finish');
  check('"Back up now" asks for a backup without passwords by default', S.calls.find((c) => c.p === '/api/backups/create').body.include_secrets === false);
  check('it shows progress while it works, then the new backup appears at the top of the list', S.calls.filter((c) => c.p === '/api/backups/status').length >= 2 && /Made by you · 2026-10-16 12:00/.test(text(host)));
  host.querySelector('#bk-secrets').checked = true; host.querySelector('[data-bk="now"]').click();
  await waitFor(() => S.calls.filter((c) => c.p === '/api/backups/create').length === 2, 'the second backup request');
  check('ticking the passwords box sends include_secrets: true', S.calls.filter((c) => c.p === '/api/backups/create')[1].body.include_secrets === true);
  await waitFor(() => S.toasts.filter((x) => /Backup saved/.test(x.text)).length === 2, 'the second backup to finish');

  // 3. Download and Delete
  host.querySelector('[data-bk="download"]').click(); await waitFor(() => S.downloads.length === 1, 'the download');
  check('Download goes straight to the backup file route, name safely encoded', S.downloads[0] === '/api/backups/' + encodeURIComponent(S.items[0].name) + '/download');
  const before = S.items.length; host.querySelectorAll('[data-bk="delete"]')[0].click();
  await waitFor(() => S.items.length < before, 'the delete');
  check('Delete asks first, then removes the file and refreshes the list', /Delete this backup file/.test(S.confirms.at(-1)) && S.calls.some((c) => c.m === 'DELETE'));

  // 4. Restore
  host.querySelector('[data-bk="restore"]').click();
  await waitFor(() => S.toasts.some((x) => /Restored 996,902 leads and 1,079,955 messages/.test(x.text)), 'the restore to finish');
  const rc = S.calls.find((c) => c.p.endsWith('/restore'));
  check('Restore asks first, saying a safety copy is saved and running campaigns come back paused', /safety copy of what is here now is saved first/.test(S.confirms.at(-1)) && /come back paused/.test(S.confirms.at(-1)));
  check('it restores the chosen backup, shows the steps, and tells how many campaigns were paused', rc && S.toasts.some((x) => /2 campaign\(s\) were paused: check them, then resume/.test(x.text)));
  check('afterwards the page reloads itself so everything shows the restored data', S.timers.includes(2500));

  // 5. Bring a backup from another computer
  const file = new w.File([new Uint8Array(1234)], 'from the old computer (1).zip', { type: 'application/zip' });
  const input = host.querySelector('#bk-file'); Object.defineProperty(input, 'files', { value: [file], configurable: true });
  input.dispatchEvent(new w.Event('change', { bubbles: true }));
  await waitFor(() => S.toasts.some((x) => /File received and checked/.test(x.text)), 'the upload');
  const up = S.calls.find((c) => c.m === 'PUT' && c.p.startsWith('/api/backups/upload'));
  check('a chosen file is sent as the raw file with its name encoded, and then listed', up && up.p.includes(encodeURIComponent('from the old computer (1).zip')) && up.body.size === 1234 && /77 leads, 5 messages, 1 campaigns/.test(text(host)), text(host).slice(0, 400));

  // 6. Saving the settings keeps the automatic-backup options
  host.querySelector('#sform [name="backup_hours"]').value = '12'; host.querySelector('#sform [name="backup_keep"]').value = '14';
  host.querySelector('#sform [name="backup_dir"]').value = 'D:\\Backups'; host.querySelector('#sform [name="backup_auto"]').checked = false;
  host.querySelector('#sform').dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true }));
  await waitFor(() => S.calls.some((c) => c.m === 'PUT' && c.p === '/api/settings'), 'the settings save');
  const sv = S.calls.find((c) => c.m === 'PUT' && c.p === '/api/settings').body;
  check('Save settings sends the backup options', String(sv.backup_hours) === '12' && String(sv.backup_keep) === '14' && sv.backup_dir === 'D:\\Backups' && sv.backup_auto === false, JSON.stringify(sv));

  // 7. A failed backup and a failed restore say so, and the page is not reloaded
  S = makeServer({ failNext: 'There is not enough free space to make a backup: it needs about 400 MB and 90 MB is free.' }); ({ w, host } = await boot(S));
  host.querySelector('[data-bk="now"]').click();
  await waitFor(() => S.toasts.some((x) => x.bad && /not enough free space/.test(x.text)), 'the failure message');
  check('a failed backup shows the plain-language reason as an error', true);
  S.failNext = 'This backup is damaged or was changed after it was made (its checksum does not match), so it was not used. Your data was not touched. Use another copy of the backup.';
  host.querySelector('[data-bk="restore"]').click();
  await waitFor(() => S.toasts.some((x) => x.bad && /checksum does not match/.test(x.text)), 'the restore failure');
  check('a refused restore says the data was not touched and does NOT reload the page', !S.timers.includes(2500));

  // 8. Notices
  S = makeServer({ notice: { at: '20261016-090000', aside: 'dericbi_crm.damaged-20261016-090000.sqlite3', restored_from: 'DericBI-backup-20261015-020000-auto.zip', error: 'x', restore_error: '' }, health: { ok: false, detail: 'row 5 missing from index' } });
  ({ w, host } = await boot(S));
  const n = host.querySelector('#bk-notice').textContent.replace(/\s+/g, ' ');
  check('after a damaged data file the panel explains what happened, what was restored and where the old file is', /was damaged and has been set aside, and your latest backup was restored/.test(n) && /dericbi_crm\.damaged-20261016/.test(n) && /Sign-in accounts are not part of a backup/.test(n), n);
  host.querySelector('[data-bk="dismiss"]').click(); await waitFor(() => S.notice === null && !host.querySelector('[data-bk="dismiss"]'), 'the notice to clear');
  check('the notice can be dismissed, and a failing health check is then shown', /health check found a problem/.test(host.querySelector('#bk-notice').textContent) && /row 5 missing from index/.test(host.querySelector('#bk-notice').textContent));

  // 9. A staff account
  S = makeServer({ owner: false }); ({ w, host } = await boot(S));
  check('a staff account is told only the owner can manage backups, and sees no buttons', /Only the owner account can manage backups/.test(host.querySelector('#bk-body').textContent) && !host.querySelector('[data-bk]'));

  const bad = results.filter((x) => !x).length; console.log(`\n${results.length - bad}/${results.length} passed`); process.exit(bad ? 1 : 0);
})().catch((e) => { console.log('FAIL (crashed): ' + e.message + '\n' + (e.stack || '').split('\n').slice(1, 4).join('\n')); process.exit(2); });
