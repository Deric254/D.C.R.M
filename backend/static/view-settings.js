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

  // Step-by-step help for the things only you can get (passwords, keys, accounts).
  const guide = (title, steps, note = '') => html`<details class="guide"><summary class="small" style="cursor:pointer;color:var(--brand,#0a6)">${title}</summary>
    <ol class="small" style="margin:8px 0 0;padding-left:20px;display:grid;gap:4px">${steps.map((x) => html`<li>${x}</li>`)}</ol>${note ? html`<p class="muted small" style="margin:8px 0 0">${note}</p>` : ''}</details>`;
  // Ready-made server values, so nobody has to know what an SMTP port is.
  const MAIL = {
    gmail: { smtp: ['smtp.gmail.com', 587, 'starttls'], imap: ['imap.gmail.com', 993, 'ssl'] },
    outlook: { smtp: ['smtp.office365.com', 587, 'starttls'], imap: ['outlook.office365.com', 993, 'ssl'] },
    zoho: { smtp: ['smtp.zoho.com', 587, 'starttls'], imap: ['imap.zoho.com', 993, 'ssl'] },
    yahoo: { smtp: ['smtp.mail.yahoo.com', 587, 'starttls'], imap: ['imap.mail.yahoo.com', 993, 'ssl'] },
  };

  // The steps of a check, each ticked or crossed, so a failure says exactly where and what to change.
  const steps = (r) => html`<ul class="small" style="list-style:none;margin:0;padding:0;display:grid;gap:4px">
    ${(r.steps || []).map((st) => html`<li style="color:${st.ok ? 'var(--accent)' : 'var(--bad)'}"><b>${st.ok ? '✓' : '✗'} ${st.label}</b>${st.detail ? html`<span style="color:var(--ink)"> ${st.detail}</span>` : ''}</li>`)}
  </ul>`;
  const showResult = (id, r, headline) => render($('#' + id), html`<div class="notice ${r.ok ? '' : 'bad'}" style="margin:0"><b>${headline || r.message}</b>${steps(r)}</div>`);

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
      let [s, health, st] = await Promise.all([api('/settings'), api('/health'), api('/status')]);
      const form = () => html`<form id="sform" class="stack" style="max-width:860px">
        <section class="panel"><div class="panel-head"><h2>Your business</h2></div><div class="panel-body">
          ${field(s, 'sender_name', 'Sender name', { hint: 'Shown on emails and used for {sender} in your messages.' })}
          <div class="grid2" style="margin-top:14px">${field(s, 'contact_website', 'Your website', { hint: 'Written into messages as {website}.' })}${field(s, 'contact_whatsapp', 'Your WhatsApp number', { hint: 'Written into messages as {whatsapp}.' })}
            ${field(s, 'contact_email', 'Your email', { hint: 'Written into messages as {email}.' })}</div></div></section>

        <section class="panel"><div class="panel-head"><h2>Logo and slogan</h2></div><div class="panel-body stack" style="gap:14px">
          <div class="row"><img id="logo-preview" class="logo-preview" src="/api/branding/logo" alt="Current logo" onerror="this.hidden=true">
            <div class="stack" style="gap:8px"><input type="file" id="logo-file" accept="image/png,image/jpeg,image/webp"><button type="button" class="btn small" id="logo-reset">Use the default logo</button></div></div>
          ${field(s, 'slogan', 'Slogan', { hint: 'Shown under the name in the sidebar. Save settings to apply.' })}
          <p class="muted small">The logo changes straight away inside the app. The program icon itself is set when the app is built: replace <code>backend/static/logo.png</code> (square PNG, 512 px or larger) and push.</p>
        </div></section>

        <section class="panel"><div class="panel-head"><h2>Email</h2><span class="chip right ${st.email_ready ? 'ok' : ''}">${st.email_ready ? (st.replies_ready ? 'Sending and replies set up' : 'Sending set up') : 'Not connected'}</span></div>
          <div class="panel-body stack" style="gap:14px">
            <p class="muted small">Connect your Gmail once and the app can send, and read the replies that come back. If anything fails you'll see exactly which step and what to change.</p>
            ${guide('Where do I get the Gmail App Password? (step by step)', [
              html`Turn on 2-Step Verification for your Google account: <a href="https://myaccount.google.com/signinoptions/two-step-verification">myaccount.google.com/signinoptions/two-step-verification</a>.`,
              html`Open <a href="https://myaccount.google.com/apppasswords">myaccount.google.com/apppasswords</a>, type the name <b>DericBI CRM</b> and press Create.`,
              'Copy the 16-letter password Google shows you and paste it below. Spaces don\'t matter. Your normal Gmail password never works here.',
              'Press Connect Gmail. It sets up sending and reading replies, then tests both.'],
              'If the app-passwords page says it is not available, 2-Step Verification is off, or your Google Workspace admin has blocked it. Also make sure IMAP is on: Gmail > Settings > See all settings > Forwarding and POP/IMAP > Enable IMAP.')}
            <div class="grid2"><label class="field">Gmail address<input type="email" id="gm-addr" value="${/@(gmail|googlemail)\./i.test(s.from_email || '') ? s.from_email : ''}" placeholder="you@gmail.com"></label>
              <label class="field">App Password<input type="password" id="gm-pass" placeholder="${s.smtp_pass_set ? 'Saved. Type to replace' : 'abcd efgh ijkl mnop'}" autocomplete="off"></label></div>
            <div class="row"><button type="button" class="btn primary" id="gm-connect">Connect Gmail</button></div>
            <div id="mail-result"></div>
            <div class="row"><input type="email" id="t-email" placeholder="Send a test email to…" style="max-width:280px" value=""><button type="button" class="btn" id="test-email">Send test email</button></div>
            <details><summary class="small muted" style="cursor:pointer">Other email provider, or change the servers by hand</summary><div class="stack" style="gap:14px;margin-top:12px">
              <label class="field">Email provider (fills in the server, port and connection for sending and reading replies)
                <select id="mail-preset"><option value="">Choose your provider…</option><option value="gmail">Gmail / Google Workspace</option><option value="outlook">Outlook / Microsoft 365</option><option value="zoho">Zoho Mail</option><option value="yahoo">Yahoo</option></select></label>
              <div class="grid2">${field(s, 'smtp_host', 'Mail server', { placeholder: 'smtp.gmail.com' })}${field(s, 'smtp_port', 'Port', { type: 'number', min: 1 })}
                ${select(s, 'smtp_security', 'Connection', [['starttls', 'STARTTLS (port 587)'], ['ssl', 'SSL (port 465)'], ['none', 'None (only for testing)']])}
                ${field(s, 'from_email', 'Send from', { placeholder: 'you@yourdomain.com' })}
                ${field(s, 'smtp_user', 'Username', { hint: 'Your full email address. Left blank, Send from is used.' })}${field(s, 'smtp_pass', 'Password', { secret: true, hint: 'For Gmail use an app password, not your normal one.' })}</div>
              ${field(s, 'reply_to', 'Reply-to address (optional)')}
              <div class="grid2">${field(s, 'imap_host', 'Reply inbox server', { placeholder: 'imap.gmail.com' })}${field(s, 'imap_port', 'Port', { type: 'number', min: 1 })}
                ${select(s, 'imap_security', 'Connection', [['ssl', 'SSL (port 993)'], ['starttls', 'STARTTLS (port 143)'], ['none', 'None (only for testing)']])}
                ${field(s, 'imap_poll_minutes', 'Check every (minutes)', { type: 'number', min: 1 })}
                ${field(s, 'imap_user', 'Inbox username', { hint: 'Left blank, the sending login is used.' })}${field(s, 'imap_pass', 'Inbox password', { secret: true, hint: 'Left blank, the sending password is used.' })}</div>
              <div><button type="button" class="btn" id="test-imap">Test reading replies</button></div>
            </div></details>
          </div></section>

        <section class="panel"><div class="panel-head"><h2>Text messages (your Android phone)</h2><span class="chip right ${st.sms_ready ? 'ok' : ''}">${st.sms_ready ? 'Phone set up' : 'Not connected'}</span></div>
          <div class="panel-body stack" style="gap:14px">
            <p class="muted small">Texts are sent by your own phone and SIM, over your home or office Wi-Fi (or the phone's hotspot). No sign-up and no per-text fee from this app; your normal SMS bundle or airtime applies.</p>
            ${guide('How do I connect my phone? (step by step)', [
              html`On the phone, install the free, open-source app <b>SMS Gateway for Android</b> (search that name on Google Play, or get it from <a href="https://github.com/capcom6/android-sms-gateway/releases">github.com/capcom6/android-sms-gateway</a>) and allow it to send SMS.`,
              'Open the app and switch on Local server, then start it. It shows the phone\'s address (like 192.168.43.1:8080), a username and a password.',
              'Put the phone and this computer on the same network: the same Wi-Fi or router, or connect this computer to the phone\'s hotspot.',
              'Type the address, username and password below, press Save settings, then Check phone, then send a test text to your own number.'],
              'If texts stop working later, the phone\'s address has probably changed after it reconnected: open the app, read the address again and update it here. Android only lets an app send about 30 texts in 30 minutes, so the app waits at least 30 seconds between texts.')}
            <div class="grid2">${field(s, 'sms_gateway_url', 'Phone address', { placeholder: '192.168.43.1:8080', hint: 'Exactly as the app shows it.' })}${field(s, 'sms_sim', 'SIM to use (optional)', { type: 'number', min: 0, hint: '0 = the phone\'s default. 1 or 2 picks a SIM on a dual-SIM phone.' })}
              ${field(s, 'sms_gateway_user', 'Username')}${field(s, 'sms_gateway_pass', 'Password', { secret: true })}</div>
            <div class="row"><button type="button" class="btn" id="check-phone">Check phone</button><input type="text" id="t-sms" placeholder="Your number, 07…" style="max-width:200px"><button type="button" class="btn" id="test-sms">Send test text</button></div>
            <div id="sms-result"></div>
          </div></section>

        <section class="panel"><div class="panel-head"><h2>WhatsApp</h2><span class="chip right ${st.whatsapp_linked ? 'ok' : ''}" id="wa-chip">${st.whatsapp_linked ? 'Connected' : 'Not connected'}</span></div>
          <div class="panel-body stack" style="gap:14px">
            <p class="muted small">Connect once by scanning a code with your phone (WhatsApp &gt; Linked devices), the same as linking WhatsApp Web. After that, campaigns send by themselves, one at a time, and every message is recorded automatically once WhatsApp shows it as sent. Until it is connected you press send yourself for each lead.</p>
            <div class="notice" style="margin:0"><b>Protect your number.</b> WhatsApp can ban a number that sends many messages to people who never chatted with it. Use a number you can afford to lose (not the one your customers already know), keep the daily limit low, and keep messages relevant.</div>
            <div class="row"><button type="button" class="btn primary" id="wa-link">${st.whatsapp_linked ? 'Connect again' : 'Connect WhatsApp'}</button>${st.whatsapp_linked ? html`<button type="button" class="btn" id="wa-unlink">Disconnect</button>` : ''}</div>
            <div id="wa-result" class="small muted"></div>
            <div class="grid2">${field(s, 'whatsapp_daily_cap', 'WhatsApp messages per day', { type: 'number', min: 0, hint: 'Start with 20 to 40.' })}${field(s, 'whatsapp_delay_sec', 'Seconds between messages', { type: 'number', min: 20, hint: 'At least 20. A random extra wait is added.' })}</div>
          </div></section>

        <section class="panel"><div class="panel-head"><h2>Limits and sending hours</h2></div>
          <div class="panel-body stack" style="gap:14px">
            <div class="grid2">${field(s, 'email_daily_cap', 'Emails per day', { type: 'number', min: 0, hint: 'Gmail allows about 500. Start low while your address builds a reputation.' })}${field(s, 'sms_daily_cap', 'Texts per day', { type: 'number', min: 0 })}
              ${field(s, 'email_delay_sec', 'Seconds between emails', { type: 'number', min: 0, hint: 'A little random variation is added.' })}${field(s, 'sms_delay_sec', 'Seconds between texts', { type: 'number', min: 30, hint: 'At least 30: Android blocks apps that send faster.' })}</div>
            ${check(s, 'send_window_enabled', 'Only send during these hours')}
            <div class="grid2">${field(s, 'send_window_start', 'From', { type: 'time' })}${field(s, 'send_window_end', 'Until', { type: 'time' })}</div>
            ${field(s, 'followup_wait_days', 'Days before a follow-up can be sent', { type: 'number', min: 0, hint: 'A lead gets one message at a time. After this many days with no reply it can be followed up.' })}
          </div></section>

        <section class="panel"><div class="panel-head"><h2>Opting out</h2></div>
          <div class="panel-body stack" style="gap:14px">
            ${check(s, 'append_optout', 'Add an opt-out line to every message')}
            <label class="field">Line added to emails<textarea name="optout_footer_email" rows="2">${s.optout_footer_email}</textarea></label>
            ${field(s, 'optout_suffix_sms', 'Text added to SMS', { hint: 'Keep it short: a text must fit in 160 characters.' })}
            <p class="muted small">Anyone who replies STOP, unsubscribe or similar (in English or Swahili) is marked do-not-contact and never messaged again. Keeping an opt-out line in place is good practice and helps with Kenya's Data Protection Act.</p>
          </div></section>

        <section class="panel"><div class="panel-head"><h2>About and updates</h2></div><div class="panel-body stack" style="gap:12px">
          <div class="small">DericBI CRM <strong>version ${health.version}</strong></div>
          <div id="update-box"></div>
        </div></section>

        <section class="panel"><div class="panel-head"><h2>AI writing help</h2></div>
          <div class="panel-body stack" style="gap:14px">
            <p class="muted small">Paste a free key from any provider below. Keys are tried in order, so when one reaches its free limit the next one answers. Use “Write with AI” when writing a campaign or a message to one lead.</p>
            ${guide('Where do I get a free AI key? (2 minutes)', [
              html`Easiest: <a href="https://aistudio.google.com/apikey">aistudio.google.com/apikey</a>. Sign in with Google, press <b>Create API key</b>, copy it into the Google Gemini box.`,
              html`A second key means the AI keeps working when the first hits its daily free limit. <a href="https://console.groq.com/keys">console.groq.com/keys</a> is also free and very fast: sign up, press <b>Create API key</b>, copy it into the Groq box.`,
              'Press Test the keys. Each key shows “works” and the model it is using, or a plain reason it failed (key rejected, limit reached, model not found).',
              'Free keys may use what you send to improve the provider\'s models, so the AI is only given the lead\'s business details and your notes, never passwords.'])}
            <label class="field">Who you are and what you sell<textarea name="ai_pitch" rows="3">${s.ai_pitch}</textarea><span class="hint">Your name, your profession, your offer and your ready-made tools. Every AI message is written from this, so keep it true.</span></label>
            <div class="grid2">${select(s, 'ai_provider', 'Try first', [...AI_KEYS.map(([id, label]) => [id, label]), ['custom', 'Your own server']])}
              ${field(s, 'ai_model', 'Model for that provider (optional)', { placeholder: (s.ai_defaults || {})[s.ai_provider] || '', hint: 'Leave blank: the app uses ' + ((s.ai_defaults || {})[s.ai_provider] || 'its default') + ' and automatically switches to a model that works if that one is retired.' })}
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
        if (e.target.id === 'mail-preset' && MAIL[e.target.value]) {
          const m = MAIL[e.target.value], f = $('#sform').elements;
          [f.smtp_host.value, f.smtp_port.value, f.smtp_security.value] = m.smtp;
          [f.imap_host.value, f.imap_port.value, f.imap_security.value] = m.imap;
          toast('Servers filled in. Add your email address and app password, then save.'); return;
        }
        const file = e.target.id === 'logo-file' && e.target.files[0]; if (!file) return;
        try { await api('/branding/logo', { method: 'PUT', body: file }); await refreshLogo(); toast('Logo updated'); } catch (err) { fail(err); }
        e.target.value = '';
      });
      el.addEventListener('click', async (e) => {
        const id = e.target.id; if (!id) return;
                try {
          if (id === 'gm-connect') {
            const address = $('#gm-addr').value.trim(), pw = $('#gm-pass').value;
            if (!address || !pw) return toast('Enter your Gmail address and the App Password', true);
            e.target.disabled = true;
            render($('#mail-result'), html`<p class="muted small">Connecting to Gmail. This can take up to half a minute…</p>`);
            const r = await api('/settings/connect-gmail', { method: 'POST', body: { address, app_password: pw }, timeout: 120000 });
            [s, st] = await Promise.all([api('/settings'), api('/status')]); draw(); App.refreshStatus && App.refreshStatus();
            showResult('mail-result', { ok: r.ok, message: r.message, steps: [...r.sending.steps, ...r.reading.steps.filter((x) => x.label !== 'Settings').map((x) => ({ ...x, label: 'Replies: ' + x.label }))] });
          } else if (id === 'test-email') {
            const to = $('#t-email').value.trim(); if (!to) return toast('Enter an address to send the test to', true);
            e.target.disabled = true; await save(); render($('#mail-result'), html`<p class="muted small">Testing…</p>`);
            showResult('mail-result', await api('/settings/test-email', { method: 'POST', body: { to }, timeout: 120000 }));
          } else if (id === 'test-imap') {
            e.target.disabled = true; await save(); render($('#mail-result'), html`<p class="muted small">Testing…</p>`);
            showResult('mail-result', await api('/settings/test-imap', { method: 'POST', timeout: 120000 }));
          } else if (id === 'check-phone') {
            e.target.disabled = true; await save(); render($('#sms-result'), html`<p class="muted small">Looking for the phone…</p>`);
            showResult('sms-result', await api('/settings/check-phone', { method: 'POST', timeout: 60000 }));
          } else if (id === 'test-sms') {
            const to = $('#t-sms').value.trim(); if (!to) return toast('Enter your own mobile number to send the test to', true);
            e.target.disabled = true; await save(); render($('#sms-result'), html`<p class="muted small">Sending a test text through the phone…</p>`);
            showResult('sms-result', await api('/settings/test-sms', { method: 'POST', body: { to }, timeout: 120000 }));
          } else if (id === 'wa-link') {
            e.target.disabled = true; await save(); await api('/whatsapp/link', { method: 'POST' });
            toast('A WhatsApp window is opening. Scan the code with your phone.', false, 7000);
            for (let i = 0; i < 180; i++) {
              await new Promise((r) => setTimeout(r, 2000));
              if (!$('#wa-result')) return;   // left this page
              const w = await api('/whatsapp/status');
              render($('#wa-result'), html`${w.link_log.map((l) => html`<div>${l}</div>`)}`);
              if (w.linked && !w.linking) { st = await api('/status'); draw(); toast('WhatsApp is connected'); return; }
              if (!w.linking) { toast('WhatsApp was not connected. Press Connect WhatsApp to try again.', true, 7000); return; }
            }
          } else if (id === 'wa-unlink') {
            if (!(await App.confirm('Disconnect WhatsApp? WhatsApp campaigns will wait until you connect again.', 'Disconnect'))) return;
            await api('/whatsapp/unlink', { method: 'POST' }); st = await api('/status'); draw(); toast('WhatsApp disconnected');
          } else if (id === 'test-ai') {
            e.target.disabled = true; await save(); const r = await api('/settings/test-ai', { method: 'POST', timeout: 60000 }); toast(r.message, !r.ok, 8000);
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
