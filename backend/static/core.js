/* Core helpers: safe templating, API client, router, status bar, dialogs, toasts.
   Everything that comes from the database (business names come from Google Maps!)
   is escaped by default through the html`` template tag. */
(function () {
  const App = (window.App = { views: {}, state: {} });

  // ------------------------------------------------------------- templating
  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ESC[c]);
  class Raw { constructor(s) { this.s = s; } toString() { return this.s; } }
  const raw = (s) => new Raw(s);
  const part = (v) => (v instanceof Raw ? v.s : Array.isArray(v) ? v.map(part).join('') : v === false || v == null ? '' : esc(v));
  const html = (strings, ...vals) => {
    let out = strings[0];
    vals.forEach((v, i) => { out += part(v) + strings[i + 1]; });
    return new Raw(out);
  };
  // Null-safe: async work often finishes after the user has already moved to another page.
  const render = (el, tpl) => { if (el) el.innerHTML = part(tpl); return el; };
  Object.assign(App, { esc, raw, html, render });

  const $ = (sel, el = document) => el.querySelector(sel);
  const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
  Object.assign(App, { $, $$ });

  const debounce = (fn, ms = 300) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
  App.debounce = debounce;

  // -------------------------------------------------------------------- API
  async function api(path, { method = 'GET', body, raw: wantRaw, timeout = 45000 } = {}) {
    const opts = { method, headers: {} };
    if (body instanceof Blob) opts.body = body;
    else if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    const ctl = new AbortController(); const timer = setTimeout(() => ctl.abort(), timeout);
    opts.signal = ctl.signal;
    let res;
    try { res = await fetch('/api' + path, opts); }
    catch (e) { throw new Error(e.name === 'AbortError' ? 'That took too long. Please try again.' : "Can't reach the CRM engine. If the app was just opened, wait a few seconds and try again."); }
    finally { clearTimeout(timer); }
    if (wantRaw) return res;
    let data = null;
    try { data = await res.json(); } catch (_) { /* empty body */ }
    if (!res.ok) {
      const err = new Error((data && data.detail) || `Request failed (${res.status})`);
      err.status = res.status; err.data = data; throw err;
    }
    return data;
  }
  App.api = api;
  const qs = (o) => {
    const p = new URLSearchParams();
    Object.entries(o).forEach(([k, v]) => { if (v !== '' && v != null && v !== false) p.set(k, v === true ? 'true' : v); });
    const s = p.toString(); return s ? '?' + s : '';
  };
  App.qs = qs;

  // ---------------------------------------------------------------- formats
  const parse = (ts) => (ts ? new Date(ts.replace(' ', 'T')) : null);
  const pad = (n) => String(n).padStart(2, '0');
  function when(ts) {
    const d = parse(ts); if (!d || isNaN(d)) return '';
    const now = new Date(); const t = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate());
    const diff = Math.round((day(now) - day(d)) / 864e5);
    if (diff === 0) return 'Today ' + t;
    if (diff === 1) return 'Yesterday ' + t;
    const mon = d.toLocaleString('en-GB', { month: 'short' });
    return `${d.getDate()} ${mon}` + (d.getFullYear() !== now.getFullYear() ? ' ' + d.getFullYear() : '') + ' ' + t;
  }
  const GRADE_LABEL = { hot: 'Hot', warm: 'Warm', cold: 'Cold', unclear: 'Needs a look', optout: 'Opted out', auto: 'Auto-reply', bounce: 'Bounced', '': '' };
  const gradeChip = (g) => (g ? html`<span class="chip ${g}">${GRADE_LABEL[g] || g}</span>` : html``);
  const STATUS_LABEL = { new: 'New', contacted: 'Contacted', replied: 'Replied', interested: 'Interested', meeting: 'Meeting', won: 'Won', lost: 'Lost' };
  const statusChip = (s) => html`<span class="chip status ${s}">${STATUS_LABEL[s] || s}</span>`;
  Object.assign(App, { when, gradeChip, statusChip, GRADE_LABEL, STATUS_LABEL });
  App.STATUSES = ['new', 'contacted', 'replied', 'interested', 'meeting', 'won', 'lost'];

  // ----------------------------------------------------------------- toasts
  function toast(msg, bad = false, ms = 4200) {
    const el = document.createElement('div');
    el.className = 'toast' + (bad ? ' bad' : ''); el.textContent = msg;
    $('#toasts').appendChild(el);
    setTimeout(() => el.remove(), bad ? Math.max(ms, 6500) : ms);
  }
  App.toast = toast;
  App.fail = (e) => toast(e.message || String(e), true);

  // Fills a message form from the AI. Whatever is already typed is sent along as guidance.
  App.aiFill = async (btn, url, channel, form) => {
    const label = btn.textContent; btn.disabled = true; btn.textContent = 'Writing…';
    try {
      const r = await api(url, { method: 'POST', body: { channel, notes: form.elements.body.value }, timeout: 100000 });
      if (form.elements.subject && r.subject) form.elements.subject.value = r.subject;
      form.elements.body.value = r.body;
      form.elements.body.dispatchEvent(new Event('input', { bubbles: true }));
    } catch (e) { App.fail(e); }
    finally { btn.disabled = false; btn.textContent = label; }
  };

  // ---------------------------------------------------------------- dialogs
  function dialog({ title, body, ok = 'OK', cancel = 'Cancel', danger = false, onOpen, wide = false }) {
    return new Promise((resolve) => {
      const dlg = $('#dlg');
      dlg.style.width = wide ? 'min(820px, 94vw)' : '';
      render(dlg, html`<form method="dialog" class="dlg-form">
        <div class="dlg-body"><h2>${title}</h2>${body}</div>
        <div class="dlg-foot">
          ${cancel ? html`<button class="btn" value="cancel" formnovalidate>${cancel}</button>` : ''}
          <button class="btn ${danger ? 'danger' : 'primary'}" value="ok" id="dlg-ok">${ok}</button>
        </div></form>`);
      dlg.onclose = () => resolve(dlg.returnValue === 'ok' ? dlg : null);
      dlg.returnValue = 'cancel';
      dlg.showModal();
      if (onOpen) onOpen(dlg);
    });
  }
  App.dialog = dialog;
  // A dialog whose Save button runs `submit(form)`; errors are shown inline and keep it open.
  App.formDialog = ({ title, body, ok = 'Save' }, submit) => new Promise((resolve) => {
    const dlg = $('#dlg');
    dlg.style.width = '';
    render(dlg, html`<form class="dlg-form" method="dialog">
      <div class="dlg-body"><h2>${title}</h2>${body}<div class="notice bad hidden" id="dlg-err" role="alert"></div></div>
      <div class="dlg-foot"><button class="btn" value="cancel" formnovalidate>Cancel</button>
      <button class="btn primary" value="ok">${ok}</button></div></form>`);
    const form = $('form', dlg);
    form.addEventListener('submit', async (e) => {
      if (e.submitter && e.submitter.value === 'cancel') return;
      e.preventDefault();
      const btn = e.submitter; btn.disabled = true;
      try { const r = await submit(form); resolve(r === undefined ? true : r); dlg.close('ok'); }
      catch (err) { const box = $('#dlg-err'); box.textContent = err.message; box.classList.remove('hidden'); btn.disabled = false; }
    });
    dlg.onclose = () => resolve(null);
    dlg.returnValue = 'cancel';
    dlg.showModal();
  });
  App.confirm = async (msg, ok = 'Confirm', danger = false) =>
    !!(await dialog({ title: 'Are you sure?', body: html`<p>${msg}</p>`, ok, danger }));

  // ----------------------------------------------------------------- router
  let cleanup = null;
  function parseHash() {
    const h = location.hash.replace(/^#\/?/, '');
    const [view, query = ''] = h.split('?');
    return { view: view || 'overview', params: Object.fromEntries(new URLSearchParams(query)) };
  }
  App.go = (view, params) => { location.hash = '#/' + view + (params ? '?' + new URLSearchParams(params) : ''); };

  async function route() {
    const { view, params } = parseHash();
    const v = App.views[view] || App.views.overview;
    const name = App.views[view] ? view : 'overview';
    if (cleanup) { try { cleanup(); } catch (_) {} cleanup = null; }
    $$('#nav a').forEach((a) => a.classList.toggle('active', a.dataset.view === name));
    $('#page-title').textContent = v.title;
    document.title = v.title + ' · DericBI CRM';
    const el = $('#content'); el.scrollTop = 0;
    render(el, html`<div class="empty">Loading…</div>`);
    try { cleanup = (await v.render(el, params)) || null; }
    catch (e) { render(el, html`<div class="notice bad">${e.message}</div>`); }
  }
  App.refresh = route;

  // ------------------------------------------------------------- status bar
  let polling = false;   // never stack requests when the engine is slow
  async function pollStatus() {
    if (polling) return;
    polling = true;
    try { await readStatus(); } finally { polling = false; }
  }
  async function readStatus() {
    try {
      const s = await api('/status');
      App.state.status = s;
      const pills = [];
      const snd = s.sender || {};
      if (s.campaigns_running || s.campaigns_paused) {
        const label = s.campaigns_paused && !s.campaigns_running ? 'Outreach paused'
          : snd.state === 'sending' ? 'Sending now' : snd.state === 'waiting' ? (snd.detail || 'Waiting') : 'Outreach running';
        pills.push(html`<span class="pill" title="${snd.detail || ''}"><i class="dot ${s.campaigns_paused && !s.campaigns_running ? 'warn' : 'on'}"></i>${label}</span>`);
      }
      if (s.job) pills.push(html`<a class="pill" href="#/find"><i class="dot on"></i>${s.job.kind === 'enrich' ? 'Finding emails' : 'Finding leads'}${s.job.progress ? ': ' + s.job.progress : ''}</a>`);
      if (App.state.update) pills.push(html`<a class="pill" href="#/settings"><i class="dot on"></i>Update ${App.state.update.version} ready</a>`);
      if (!s.email_ready && !s.sms_ready && !s.whatsapp_linked) pills.push(html`<a class="pill" href="#/settings"><i class="dot warn"></i>Connect email, WhatsApp or your phone</a>`);
      render($('#pills'), html`${pills}`);
      const b = $('#badge-replies');
      b.textContent = s.inbox_unhandled; b.classList.toggle('hidden', !s.inbox_unhandled);
    } catch (_) {
      render($('#pills'), html`<span class="pill"><i class="dot warn"></i>Engine not responding</span>`);
    }
  }
  App.pollStatus = pollStatus;

  // ----------------------------------------------------- branding + updates
  // A missing or unreadable logo just hides the image; it never gets in the way.
  // The server revalidates the logo on every load, so only add a changing suffix right after the logo is replaced.
  let logoRev = '';
  App.showLogo = (img, changed) => { if (changed) logoRev = '?v=' + Date.now(); img.hidden = false; img.src = '/api/branding/logo' + logoRev; };
  App.loadBrand = async () => {
    const [s, h] = await Promise.all([api('/settings'), api('/health')]);
    const slogan = $('#slogan');
    slogan.textContent = s.slogan; slogan.classList.toggle('hidden', !s.slogan);
    $('#side-foot').textContent = 'v' + h.version;
    App.showLogo($('#brand-logo'));
  };
  // Only the desktop shell can update itself; in a plain browser these stay unavailable.
  const shell = () => window.__TAURI__ && window.__TAURI__.core;
  App.desktop = () => !!shell();
  App.checkUpdate = async () => (App.state.update = await shell().invoke('check_update'));
  App.installUpdate = () => shell().invoke('install_update');

  App.start = function () {
    window.addEventListener('hashchange', route);
    // Links that leave the app open in the normal browser, so the app window never navigates away.
    document.addEventListener('click', (e) => {
      const a = e.target.closest('a[href]'); if (!a) return;
      const href = a.getAttribute('href') || '';
      if (/^https?:\/\//i.test(href) && !href.startsWith(location.origin)) {
        e.preventDefault();
        api('/open-url', { method: 'POST', body: { url: href } }).catch(fail);
      }
    });
    $('#dlg').addEventListener('click', (e) => { if (e.target === $('#dlg')) $('#dlg').close('cancel'); });
    pollStatus(); setInterval(pollStatus, 4000);
    App.loadBrand().catch(() => {});
    if (App.desktop()) setTimeout(() => App.checkUpdate().catch(() => {}), 6000);
    route();
  };

  // helpers used by several views
  App.copy = async (text, msg = 'Copied') => { try { await navigator.clipboard.writeText(text); toast(msg); } catch (_) { toast('Copy failed. Select the text and press Ctrl+C.', true); } };
  App.download = (url) => { const a = document.createElement('a'); a.href = url; a.download = ''; document.body.appendChild(a); a.click(); a.remove(); };
  App.pct = (a, b) => (b ? Math.min(100, Math.round((100 * a) / b)) : 0);
  App.contactLine = (l) => [l.phone_display, l.email].filter(Boolean).join('  ·  ');
})();
