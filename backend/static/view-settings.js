(function () {
  const { html, render, api, $, $$, toast, fail } = App;

  const field = (s, key, label, { type = 'text', hint = '', placeholder = '', secret = false, min } = {}) => html`
    <label class="field">${label}
      <input type="${secret ? 'password' : type}" name="${key}" value="${secret ? '' : s[key]}" ${secret && s[key + '_set'] ? html`placeholder="Saved. Type to replace"` : placeholder ? html`placeholder="${placeholder}"` : ''} ${min !== undefined ? html`min="${min}"` : ''} autocomplete="off">
      ${hint ? html`<span class="hint">${hint}</span>` : ''}</label>`;
  const select = (s, key, label, options, hint = '') => html`
    <label class="field">${label}<select name="${key}">${options.map(([v, l]) => html`<option value="${v}" ${String(s[key]) === v ? 'selected' : ''}>${l}</option>`)}</select>${hint ? html`<span class="hint">${hint}</span>` : ''}</label>`;
  const AI_KEYS = [['gemini', 'Google Gemini', 'https://aistudio.google.com/apikey'], ['groq', 'Groq', 'https://console.groq.com/keys'],
    ['nvidia', 'NVIDIA', 'https://build.nvidia.com/'], ['openrouter', 'OpenRouter', 'https://openrouter.ai/keys'], ['mistral', 'Mistral', 'https://console.mistral.ai/api-keys']];
  const check = (s, key, label) => html`<label class="check"><input type="checkbox" name="${key}" ${s[key] ? 'checked' : ''}>${label}</label>`;

  function collect(form) {
    const out = {};
    [...form.elements].forEach((e) => {
      if (!e.name) return;
      if (e.type === 'checkbox') out[e.name] = e.checked; else out[e.name] = e.value;
    });
    return out;
  }

  App.views.settings = {
    title: 'Settings',
    async render(el) {
      let [s, health] = await Promise.all([api('/settings'), api('/health')]);
      const form = () => html`<form id="sform" class="stack" style="max-width:860px">
        <section class="panel"><div class="panel-head"><h2>Your business</h2></div><div class="panel-body">
          ${field(s, 'sender_name', 'Sender name', { hint: 'Shown on emails and used for {sender} in your messages.' })}</div></section>

        <section class="panel"><div class="panel-head"><h2>Logo and slogan</h2></div><div class="panel-body stack" style="gap:14px">
          <div class="row"><img id="logo-preview" class="logo-preview" src="/api/branding/logo" alt="Current logo" onerror="this.hidden=true">
            <div class="stack" style="gap:8px"><input type="file" id="logo-file" accept="image/png,image/jpeg,image/webp"><button type="button" class="btn small" id="logo-reset">Use the default logo</button></div></div>
          ${field(s, 'slogan', 'Slogan', { hint: 'Shown under the name in the sidebar. Save settings to apply.' })}
          <p class="muted small">The logo changes straight away inside the app. The program icon itself is set when the app is built: replace <code>backend/static/logo.png</code> (square PNG, 512 px or larger) and push.</p>
        </div></section>

        <section class="panel"><div class="panel-head"><h2>Sending email</h2><button type="button" class="btn small right" id="gmail-smtp">Fill in Gmail settings</button></div>
          <div class="panel-body stack" style="gap:14px">
            <div class="grid2">${field(s, 'smtp_host', 'Mail server', { placeholder: 'smtp.gmail.com' })}${field(s, 'smtp_port', 'Port', { type: 'number', min: 1 })}
              ${select(s, 'smtp_security', 'Connection', [['starttls', 'STARTTLS (port 587)'], ['ssl', 'SSL (port 465)'], ['none', 'None (only for testing)']])}
              ${field(s, 'from_email', 'Send from', { placeholder: 'you@yourdomain.com' })}
              ${field(s, 'smtp_user', 'Username', { hint: 'Usually your full email address.' })}${field(s, 'smtp_pass', 'Password', { secret: true, hint: 'For Gmail use an app password, not your normal one.' })}</div>
            ${field(s, 'reply_to', 'Reply-to address (optional)')}
            <div class="row"><input type="email" id="t-email" placeholder="Send a test email to…" style="max-width:280px" value=""><button type="button" class="btn" id="test-email">Send test email</button></div>
          </div></section>

        <section class="panel"><div class="panel-head"><h2>Reading replies</h2><button type="button" class="btn small right" id="gmail-imap">Fill in Gmail settings</button></div>
          <div class="panel-body stack" style="gap:14px">
            <p class="muted small">Connect the inbox your leads reply to so their answers are matched, graded and bounces are caught. Only mail that arrives after you connect is tracked.</p>
            <div class="grid2">${field(s, 'imap_host', 'Inbox server', { placeholder: 'imap.gmail.com' })}${field(s, 'imap_port', 'Port', { type: 'number', min: 1 })}
              ${select(s, 'imap_security', 'Connection', [['ssl', 'SSL (port 993)'], ['starttls', 'STARTTLS (port 143)'], ['none', 'None (only for testing)']])}
              ${field(s, 'imap_poll_minutes', 'Check every (minutes)', { type: 'number', min: 1 })}
              ${field(s, 'imap_user', 'Username')}${field(s, 'imap_pass', 'Password', { secret: true })}</div>
            <div><button type="button" class="btn" id="test-imap">Test connection</button></div>
          </div></section>

        <section class="panel"><div class="panel-head"><h2>Text messages (Africa's Talking)</h2></div>
          <div class="panel-body stack" style="gap:14px">
            <div class="grid2">${field(s, 'at_username', 'Username', { hint: 'Use “sandbox” to try it without sending real texts.' })}${field(s, 'at_api_key', 'API key', { secret: true })}
              ${field(s, 'at_sender_id', 'Sender ID (optional)', { hint: 'Needs approval from Africa\'s Talking. Leave blank to use their default.' })}</div>
            <details><summary class="small muted" style="cursor:pointer">Advanced</summary><div style="margin-top:10px">${field(s, 'at_base_url', 'API address')}</div></details>
            <div class="row"><input type="text" id="t-sms" placeholder="Send a test text to 07…" style="max-width:240px"><button type="button" class="btn" id="test-sms">Send test text</button></div>
          </div></section>

        <section class="panel"><div class="panel-head"><h2>Limits and sending hours</h2></div>
          <div class="panel-body stack" style="gap:14px">
            <div class="grid2">${field(s, 'email_daily_cap', 'Emails per day', { type: 'number', min: 0, hint: 'Gmail allows about 500. Start low while your address builds a reputation.' })}${field(s, 'sms_daily_cap', 'Texts per day', { type: 'number', min: 0 })}
              ${field(s, 'email_delay_sec', 'Seconds between emails', { type: 'number', min: 0, hint: 'A little random variation is added.' })}${field(s, 'sms_delay_sec', 'Seconds between texts', { type: 'number', min: 0 })}</div>
            ${check(s, 'send_window_enabled', 'Only send during these hours')}
            <div class="grid2">${field(s, 'send_window_start', 'From', { type: 'time' })}${field(s, 'send_window_end', 'Until', { type: 'time' })}</div>
            ${field(s, 'skip_recent_days', 'Don\'t message anyone contacted in the last (days)', { type: 'number', min: 0 })}
          </div></section>

        <section class="panel"><div class="panel-head"><h2>Opting out</h2></div>
          <div class="panel-body stack" style="gap:14px">
            ${check(s, 'append_optout', 'Add an opt-out line to every message')}
            <label class="field">Line added to emails<textarea name="optout_footer_email" rows="2">${s.optout_footer_email}</textarea></label>
            ${field(s, 'optout_suffix_sms', 'Text added to SMS')}
            <p class="muted small">Anyone who replies STOP, unsubscribe or similar (in English or Swahili) is marked do-not-contact and never messaged again. Keeping an opt-out line in place is good practice and helps with Kenya's Data Protection Act.</p>
          </div></section>

        <section class="panel"><div class="panel-head"><h2>About and updates</h2></div><div class="panel-body stack" style="gap:12px">
          <div class="small">DericBI CRM <strong>version ${health.version}</strong></div>
          <div id="update-box"></div>
        </div></section>

        <section class="panel"><div class="panel-head"><h2>AI writing help</h2></div>
          <div class="panel-body stack" style="gap:14px">
            <p class="muted small">Paste a free key from any provider below. Keys are tried in order, so when one reaches its free limit the next one answers. Use “Write with AI” when writing a campaign or a message to one lead.</p>
            <label class="field">Who you are and what you sell<textarea name="ai_pitch" rows="3">${s.ai_pitch}</textarea><span class="hint">Your name, your profession, your offer and your ready-made tools. Every AI message is written from this, so keep it true.</span></label>
            <div class="grid2">${select(s, 'ai_provider', 'Try first', [...AI_KEYS.map(([id, label]) => [id, label]), ['custom', 'Your own server']])}
              ${field(s, 'ai_model', 'Model for that provider (optional)', { hint: 'Leave blank to use the default.' })}
              ${AI_KEYS.map(([id, label, url]) => field(s, 'ai_key_' + id, label + ' key', { secret: true, hint: html`Free key: <a href="${url}">${url.replace('https://', '')}</a>` }))}</div>
            <details><summary class="small muted" style="cursor:pointer">Your own server (any OpenAI-compatible address, such as Ollama)</summary>
              <div class="grid2" style="margin-top:10px">${field(s, 'ai_custom_url', 'Address', { placeholder: 'http://localhost:11434/v1' })}${field(s, 'ai_custom_model', 'Model')}${field(s, 'ai_key_custom', 'Key (if it needs one)', { secret: true })}</div></details>
            ${check(s, 'ai_grade_replies', 'Let the AI grade replies that the built-in rules cannot read')}
            <div><button type="button" class="btn" id="test-ai">Test the keys</button></div>
          </div></section>

        <section class="panel"><div class="panel-head"><h2>Your data</h2></div>
          <div class="panel-body stack" style="gap:12px">
            <div class="small muted">Everything is stored on this computer in <code>${health.data_dir}</code>. Passwords are stored there too, so keep backups private.</div>
            <div><button type="button" class="btn" id="backup">Download a backup</button></div>
            <details><summary class="small muted" style="cursor:pointer">Advanced: incoming SMS webhook</summary><div class="stack" style="gap:10px;margin-top:10px">
              <p class="muted small">Only useful if you expose this app to the internet (for example with a tunnel) and point Africa's Talking's incoming-message callback at it. Most people just use “Log a reply”.</p>
              ${field(s, 'webhook_token', 'Secret token')}
              <div class="small muted">Callback address: <code>http://127.0.0.1:8765/api/webhooks/sms?token=YOUR_TOKEN</code> (replace the host with your public address)</div>
            </div></details>
          </div></section>

        <div class="row"><button class="btn primary" type="submit">Save settings</button><span class="muted small" id="saved"></span></div>
      </form>`;
      const updateBox = () => render($('#update-box'), !App.desktop()
        ? html`<p class="muted small">Updates are installed from the desktop app.</p>`
        : html`<div class="row"><button type="button" class="btn" id="check-update">Check for updates</button>
            ${App.state.update ? html`<button type="button" class="btn primary" id="install-update">Install version ${App.state.update.version} and restart</button>` : ''}</div>`);
      const draw = () => { render(el, form()); updateBox(); };
      draw();

      const save = async () => {
        s = await api('/settings', { method: 'PUT', body: collect($('#sform')) });
        return s;
      };
      el.addEventListener('submit', async (e) => {
        e.preventDefault();
        try { await save(); toast('Settings saved'); draw(); App.loadBrand(); } catch (err) { fail(err); }
      });
      const refreshLogo = () => { App.showLogo($('#logo-preview'), true); return App.loadBrand(); };
      el.addEventListener('change', async (e) => {
        const file = e.target.id === 'logo-file' && e.target.files[0]; if (!file) return;
        try { await api('/branding/logo', { method: 'PUT', body: file }); await refreshLogo(); toast('Logo updated'); } catch (err) { fail(err); }
        e.target.value = '';
      });
      el.addEventListener('click', async (e) => {
        const id = e.target.id; if (!id) return;
        const set = (name, v) => { $('#sform').elements[name].value = v; };
        try {
          if (id === 'gmail-smtp') { set('smtp_host', 'smtp.gmail.com'); set('smtp_port', 587); set('smtp_security', 'starttls'); toast('Add your Gmail address and an app password, then save.'); }
          else if (id === 'gmail-imap') { set('imap_host', 'imap.gmail.com'); set('imap_port', 993); set('imap_security', 'ssl'); }
          else if (id === 'test-email') {
            const to = $('#t-email').value.trim(); if (!to) return toast('Enter an address to send the test to', true);
            e.target.disabled = true; await save(); const r = await api('/settings/test-email', { method: 'POST', body: { to } }); toast(r.message);
          } else if (id === 'test-sms') {
            const to = $('#t-sms').value.trim(); if (!to) return toast('Enter a mobile number to send the test to', true);
            e.target.disabled = true; await save(); const r = await api('/settings/test-sms', { method: 'POST', body: { to } }); toast(r.message);
          } else if (id === 'test-ai') {
            e.target.disabled = true; await save(); const r = await api('/settings/test-ai', { method: 'POST', timeout: 60000 }); toast(r.message, !r.ok, 8000);
          } else if (id === 'test-imap') {
            e.target.disabled = true; await save(); const r = await api('/settings/test-imap', { method: 'POST' }); toast(r.message);
          } else if (id === 'backup') App.download('/api/backup');
          else if (id === 'logo-reset') { await api('/branding/logo', { method: 'DELETE' }); await refreshLogo(); toast('Default logo restored'); }
          else if (id === 'check-update') {
            e.target.disabled = true; const u = await App.checkUpdate(); updateBox();
            toast(u ? `Version ${u.version} is available` : 'You have the latest version');
          } else if (id === 'install-update') {
            e.target.disabled = true; toast('Downloading the update. The app restarts by itself when it is ready.'); await App.installUpdate();
          }
        } catch (err) { fail(err); }
        finally { if (e.target.tagName === 'BUTTON') e.target.disabled = false; }
      });
    },
  };
})();
