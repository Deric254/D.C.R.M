(function () {
  const { html, render, api, $, $$, toast, fail, debounce } = App;
  let onDone = null, relocking = false;

  const shell = (inner) => render($('#auth'), html`<div class="auth-card"><div class="auth-brand"><img src="/api/branding/logo" alt="" onerror="this.hidden=true"><div><b>DericBI CRM</b><div class="small muted">One lead at a time</div></div></div>${inner}</div>`);
  const errBox = (msg) => render($('#auth-err'), msg ? html`<div class="notice bad" style="margin:0">${msg}</div>` : html``);

  // ------------------------------------------------------------ shared pieces
  // A strong password the person can accept or type over. The server's own rules judge every candidate.
  const SETS = ['ABCDEFGHJKLMNPQRSTUVWXYZ', 'abcdefghijkmnpqrstuvwxyz', '23456789', '!?#%@'];
  const pick = (s) => s[crypto.getRandomValues(new Uint32Array(1))[0] % s.length];
  const generate = () => {
    const all = SETS.join(''); const out = SETS.map(pick);
    while (out.length < 16) out.push(pick(all));
    for (let i = out.length - 1; i > 0; i--) { const j = crypto.getRandomValues(new Uint32Array(1))[0] % (i + 1); [out[i], out[j]] = [out[j], out[i]]; }
    return out.join('');
  };
  const passwordBlock = (p, label = 'Password') => html`
    <label class="field">${label}<input type="password" id="${p}-pw" autocomplete="new-password"></label>
    <label class="field">Type it again<input type="password" id="${p}-pw2" autocomplete="new-password"></label>
    <div class="row"><button type="button" class="btn small" id="${p}-suggest">Suggest a strong password</button><label class="opt small"><input type="checkbox" id="${p}-show">Show</label></div>
    <div id="${p}-hint" class="small muted">At least 12 characters, with a capital letter, a number and a symbol.</div>`;
  const bindPassword = (p, username) => {
    const pw = $('#' + p + '-pw'), pw2 = $('#' + p + '-pw2'), hint = $('#' + p + '-hint');
    const who = () => (typeof username === 'function' ? username() : username) || '';
    const check = debounce(async () => {
      if (!pw.value) return render(hint, html`At least 12 characters, with a capital letter, a number and a symbol.`);
      const r = await api('/auth/password-check', { method: 'POST', body: { password: pw.value, username: who() } }).catch(() => ({ problems: [] }));
      const mismatch = pw2.value && pw2.value !== pw.value;
      render(hint, r.problems.length ? html`<ul style="margin:0;padding-left:18px;color:var(--warm)">${r.problems.map((x) => html`<li>${x}</li>`)}</ul>`
        : html`<span style="color:var(--accent)">✓ Strong password.</span>${mismatch ? html` <span style="color:var(--warm)">The two boxes don't match yet.</span>` : ''}`);
    }, 250);
    pw.addEventListener('input', check); pw2.addEventListener('input', check);
    $('#' + p + '-show').onchange = (e) => { pw.type = pw2.type = e.target.checked ? 'text' : 'password'; };
    $('#' + p + '-suggest').onclick = async () => {
      let s = generate();
      for (let i = 0; i < 20; i++) {
        const r = await api('/auth/password-check', { method: 'POST', body: { password: s, username: who() } });
        if (!r.problems.length) break; s = generate();
      }
      pw.value = pw2.value = s; $('#' + p + '-show').checked = true; pw.type = pw2.type = 'text'; check();
      toast('Write this password down somewhere safe before you continue', false, 6000);
    };
  };
  const readPassword = (p) => {
    const a = $('#' + p + '-pw').value, b = $('#' + p + '-pw2').value;
    if (!a) throw new Error('Choose a password.');
    if (a !== b) throw new Error("The two passwords don't match.");
    return a;
  };
  const questionBlock = (p, questions) => html`<div class="stack" style="gap:8px"><div class="small muted">Choose three security questions. If you forget your password, all three answers are needed to get back in. Pick answers only you know that people can't find online.</div>
    ${[0, 1, 2].map((i) => html`<div class="field"><select id="${p}-q${i}">${questions.map((q, j) => html`<option value="${q}" ${j === i ? 'selected' : ''}>${q}</option>`)}</select><input type="text" id="${p}-a${i}" placeholder="Your answer" autocomplete="off"></div>`)}</div>`;
  const readQuestions = (p) => [0, 1, 2].map((i) => ({ q: $('#' + p + '-q' + i).value, a: $('#' + p + '-a' + i).value }));
  const submitter = (btn, job) => async () => {
    btn.disabled = true; errBox('');
    try { await job(); } catch (e) { errBox(e.message); btn.disabled = false; }
  };

  // ------------------------------------------------------------------ screens
  async function show() {
    document.body.classList.add('locked');
    const st = await api('/auth/status');
    App.auth = st;
    if (st.state === 'ok') {
      if (st.user.must_change || st.user.needs_questions) return finishScreen(st);
      return done(st);
    }
    return st.state === 'setup' ? setupScreen(st) : loginScreen(st);
  }
  function done(st) {
    document.body.classList.remove('locked');
    render($('#auth'), html``);
    fillWho(st);
    if (onDone) onDone();
  }

  function setupScreen(st) {
    let mode = 'owner';
    const draw = () => {
      shell(html`<h2 style="margin:0 0 4px">Welcome</h2><p class="muted small" style="margin:0 0 14px">Create the first account on this computer.</p>
        <div class="chips" style="margin-bottom:14px"><label class="opt"><input type="radio" name="mode" value="owner" ${mode === 'owner' ? 'checked' : ''}>This is the owner's computer</label>
          <label class="opt"><input type="radio" name="mode" value="member" ${mode === 'member' ? 'checked' : ''}>The owner gave me access</label></div>
        <form id="sform" class="stack" style="gap:14px" autocomplete="off">
          ${mode === 'owner' ? html`
            <label class="field">Username<input type="text" value="${st.owner_name}" readonly></label>
            ${st.licensing_configured ? html`<label class="field">Owner key<textarea id="s-key" rows="2" placeholder="DBI-OWNER-…  (from your dericbi-owner.key file)" spellcheck="false"></textarea><span class="hint">This proves the computer is yours. It is never saved.</span></label>`
              : html`<div class="notice" style="margin:0"><b>Owner key not set in this build.</b> You can create your account now, but other computers can't be licensed until you run <code>scripts/make-owner-key.py</code>, paste the public key into <code>backend/owner_key.py</code> and rebuild.</div>`}`
          : html`
            <div class="notice" style="margin:0"><b>This computer's activation code</b><div style="font-size:20px;letter-spacing:.08em;margin:6px 0"><code id="s-code">${st.install_id}</code> <button type="button" class="btn small" id="s-copy">Copy</button></div>
              Send this code to the owner (WhatsApp is fine). They will send you back a licence: paste it below.</div>
            ${!st.licensing_configured ? html`<div class="notice bad" style="margin:0">This copy of the app can't check licences, so it can't be activated. Ask the owner for a newer installer.</div>` : ''}
            <label class="field">Licence from the owner<textarea id="s-lic" rows="3" placeholder="DBI1.…" spellcheck="false"></textarea><span class="hint">Your username is set by the licence.</span></label>`}
          ${passwordBlock('s')}
          ${questionBlock('s', st.questions)}
          <div id="auth-err"></div>
          <button class="btn primary" type="submit" id="s-go">${mode === 'owner' ? 'Create owner account' : 'Activate this computer'}</button>
        </form>`);
      $$('input[name=mode]').forEach((r) => (r.onchange = () => { mode = r.value; draw(); }));
      bindPassword('s', () => (mode === 'owner' ? st.owner_name : ''));
      if ($('#s-copy')) $('#s-copy').onclick = () => App.copy(st.install_id, 'Activation code copied');
      $('#sform').onsubmit = (e) => {
        e.preventDefault();
        submitter($('#s-go'), async () => {
          const body = { mode, username: mode === 'owner' ? st.owner_name : '', password: readPassword('s'), questions: readQuestions('s'),
            owner_key: $('#s-key') ? $('#s-key').value : '', license: $('#s-lic') ? $('#s-lic').value : '' };
          const r = await api('/auth/setup', { method: 'POST', body, timeout: 60000 });
          if (r.warning) toast(r.warning, true, 12000);
          await show();
        })();
      };
    };
    draw();
  }

  function loginScreen(st) {
    const lic = st.license && !st.license.ok ? st.license : null;
    shell(html`<h2 style="margin:0 0 14px">Sign in</h2>
      <form id="lform" class="stack" style="gap:14px">
        <label class="field">Username<input type="text" id="l-user" autocomplete="username" value="${st.kind === 'owner' ? st.owner_name : ''}"></label>
        <label class="field">Password<input type="password" id="l-pw" autocomplete="current-password"></label>
        <div id="auth-err"></div>
        <button class="btn primary" type="submit" id="l-go">Sign in</button>
        <div class="row"><a href="#" id="l-forgot" class="small">Forgot your password?</a></div>
      </form>
      ${lic ? html`<div class="stack" style="gap:8px;margin-top:16px"><div class="notice bad" style="margin:0">${lic.message}</div>
        <div class="small muted">Activation code for this computer: <code>${st.install_id}</code></div>
        <textarea id="l-lic" rows="2" placeholder="Paste the new licence from the owner" spellcheck="false"></textarea>
        <div><button class="btn small" id="l-renew">Use this licence</button></div></div>` : ''}`);
    $(st.kind === 'owner' ? '#l-pw' : '#l-user').focus();
    $('#lform').onsubmit = (e) => {
      e.preventDefault();
      submitter($('#l-go'), async () => { await api('/auth/login', { method: 'POST', body: { username: $('#l-user').value, password: $('#l-pw').value } }); await show(); })();
    };
    $('#l-forgot').onclick = (e) => { e.preventDefault(); recoverScreen(st); };
    if ($('#l-renew')) $('#l-renew').onclick = async () => {
      try { await api('/auth/renew-license', { method: 'POST', body: { license: $('#l-lic').value } }); toast('Licence updated'); await show(); } catch (err) { errBox(err.message); }
    };
  }

  function recoverScreen(st) {
    shell(html`<h2 style="margin:0 0 4px">Forgot your password?</h2><p class="muted small" style="margin:0 0 14px">Answer your three security questions to choose a new password.</p>
      <form id="rform" class="stack" style="gap:14px"><label class="field">Username<input type="text" id="r-user" value="${st.kind === 'owner' ? st.owner_name : ''}"></label>
        <div id="r-step"></div><div id="auth-err"></div>
        <div class="row"><button class="btn primary" type="submit" id="r-go">Show my questions</button><a href="#" id="r-back" class="small">Back to sign in</a></div></form>`);
    let questions = null, ownerKey = false;
    $('#r-back').onclick = (e) => { e.preventDefault(); loginScreen(st); };
    $('#rform').onsubmit = (e) => {
      e.preventDefault();
      submitter($('#r-go'), async () => {
        const user = $('#r-user').value;
        if (!questions) {
          const r = await api('/auth/recover-questions', { method: 'POST', body: { username: user } });
          questions = r.questions; ownerKey = r.owner && st.licensing_configured;
          render($('#r-step'), html`<div class="stack" style="gap:10px">${questions.map((q, i) => html`<label class="field">${q}<input type="text" id="r-a${i}" autocomplete="off"></label>`)}
            ${ownerKey ? html`<label class="field">Owner key<textarea id="r-key" rows="2" placeholder="DBI-OWNER-…" spellcheck="false"></textarea><span class="hint">The owner account also needs the owner key.</span></label>` : ''}
            ${passwordBlock('r', 'New password')}</div>`);
          bindPassword('r', user);
          $('#r-user').readOnly = true; $('#r-go').textContent = 'Set new password'; $('#r-go').disabled = false;
          return;
        }
        await api('/auth/recover', { method: 'POST', body: { username: user, answers: [0, 1, 2].map((i) => $('#r-a' + i).value), new_password: readPassword('r'), owner_key: ownerKey ? $('#r-key').value : '' } });
        toast('Password changed. Sign in with your new password.', false, 6000);
        loginScreen(await api('/auth/status'));
      })();
    };
  }

  // A person the owner added signs in once with a temporary password, then must choose their own.
  function finishScreen(st) {
    const u = st.user;
    shell(html`<h2 style="margin:0 0 4px">Set up your account</h2><p class="muted small" style="margin:0 0 14px">Hello ${u.username}. ${u.must_change ? 'Choose your own password' : 'Choose your security questions'} to continue.</p>
      <form id="fform" class="stack" style="gap:14px">
        <label class="field">${u.must_change ? 'The temporary password you were given' : 'Your password'}<input type="password" id="f-cur" autocomplete="current-password"></label>
        ${u.must_change ? passwordBlock('f', 'Your new password') : ''}
        ${u.needs_questions ? questionBlock('f', st.questions) : ''}
        <div id="auth-err"></div>
        <div class="row"><button class="btn primary" type="submit" id="f-go">Continue</button><a href="#" id="f-out" class="small">Sign out</a></div></form>`);
    if (u.must_change) bindPassword('f', u.username);
    $('#f-out').onclick = async (e) => { e.preventDefault(); await api('/auth/logout', { method: 'POST' }); location.reload(); };
    $('#fform').onsubmit = (e) => {
      e.preventDefault();
      submitter($('#f-go'), async () => {
        let current = $('#f-cur').value;
        if (u.must_change) {
          const next = readPassword('f');
          await api('/auth/change-password', { method: 'POST', body: { current, new: next } }); current = next;
        }
        if (u.needs_questions) await api('/auth/change-questions', { method: 'POST', body: { current, questions: readQuestions('f') } });
        await show();
      })();
    };
  }

  // -------------------------------------------------- who is signed in, sign out
  function fillWho(st) {
    const u = st.user, lic = st.license;
    const soon = lic && lic.ok && lic.days_left <= 14;
    render($('#who'), html`<div class="who"><div><b>${u.username}</b><small>${u.role === 'owner' ? 'Owner' : 'Member'}</small></div><button class="btn small quiet" id="signout" title="Sign out">Sign out</button></div>
      ${soon ? html`<a class="small" href="#/settings" style="color:var(--warm)">Licence ends in ${lic.days_left} day${lic.days_left === 1 ? '' : 's'}</a>` : ''}`);
    $('#signout').onclick = async () => { await api('/auth/logout', { method: 'POST' }).catch(() => {}); location.reload(); };
  }

  App.gate = () => new Promise((resolve) => { onDone = () => resolve(true); show().catch((e) => render($('#auth'), html`<div class="auth-card"><div class="notice bad">${e.message}</div></div>`)); });
  // Any call that finds the session gone (signed out elsewhere, idle too long, licence ended) brings the sign-in back.
  App.relock = () => { if (relocking) return; relocking = true; onDone = () => location.reload(); show(); };

  // ---------------------------------------------------- Settings: account + people
  App.renderAccount = async function (box) {
    const st = await api('/auth/status');
    const u = st.user, lic = st.license;
    const people = u.role === 'owner' ? await api('/users') : [];
    const draw = async () => {
      render(box, html`
        <section class="panel"><div class="panel-head"><h2>Your account</h2><span class="chip right ok">${u.role === 'owner' ? 'Owner' : 'Member'}</span></div>
          <div class="panel-body stack" style="gap:16px">
            <div>Signed in as <b>${u.username}</b>.</div>
            ${lic ? html`<div class="notice ${lic.ok ? '' : 'bad'}" style="margin:0">${lic.ok ? html`Licence valid until <b>${lic.expires}</b> (${lic.days_left} day${lic.days_left === 1 ? '' : 's'} left).` : lic.message}
              <div class="stack" style="gap:6px;margin-top:8px"><div class="small muted">Activation code for this computer: <code>${st.install_id}</code></div>
                <textarea id="a-lic" rows="2" placeholder="Paste a new licence from the owner to extend this one" spellcheck="false"></textarea>
                <div><button class="btn small" id="a-renew">Use this licence</button></div></div></div>` : ''}
            <form id="a-pform" class="stack" style="gap:12px;max-width:460px"><h3>Change your password</h3>
              <label class="field">Current password<input type="password" id="a-cur" autocomplete="current-password"></label>
              ${passwordBlock('a', 'New password')}
              <div id="a-perr"></div><div><button class="btn primary" type="submit" id="a-pgo">Change password</button></div></form>
            <details><summary class="small" style="cursor:pointer">Change my security questions</summary>
              <form id="a-qform" class="stack" style="gap:12px;max-width:460px;margin-top:12px">
                <label class="field">Your password<input type="password" id="a-qcur" autocomplete="current-password"></label>
                ${questionBlock('aq', st.questions)}
                <div id="a-qerr"></div><div><button class="btn" type="submit" id="a-qgo">Save questions</button></div></form></details>
          </div></section>
        ${u.role === 'owner' ? html`
        <section class="panel"><div class="panel-head"><h2>People on this computer</h2></div>
          <div class="panel-body stack" style="gap:14px">
            <div class="tablewrap"><table class="t"><thead><tr><th>Name</th><th>Role</th><th>Last signed in</th><th></th></tr></thead><tbody>
              ${people.map((p) => html`<tr style="cursor:default"><td><b>${p.username}</b>${p.active ? '' : html` <span class="chip cold">Switched off</span>`}${p.must_change ? html` <span class="chip warm">Has a temporary password</span>` : ''}</td>
                <td>${p.role === 'owner' ? 'Owner' : 'Member'}</td><td class="small muted">${p.last_login_at || 'Never'}</td>
                <td class="nowrap">${p.role === 'owner' ? '' : html`<button class="btn small" data-u="reset" data-id="${p.id}">New temporary password</button>
                  <button class="btn small" data-u="active" data-id="${p.id}" data-on="${p.active ? 0 : 1}">${p.active ? 'Switch off' : 'Switch on'}</button>
                  <button class="btn small danger" data-u="remove" data-id="${p.id}">Remove</button>`}</td></tr>`)}</tbody></table></div>
            <form id="a-add" class="row"><input type="text" id="a-newname" placeholder="Name of the new person" style="max-width:260px"><button class="btn primary" type="submit">Add person</button></form>
            <div id="a-temp"></div>
            <div class="small muted">A new person gets a temporary password. On first sign-in they choose their own password and security questions.</div>
          </div></section>
        <section class="panel"><div class="panel-head"><h2>Let another computer use the app</h2></div>
          <div class="panel-body stack" style="gap:12px;max-width:560px">
            <div class="small muted">Another computer can't be used until you sign a licence for it. On that computer, the first screen shows its activation code. Enter it here, add the person's name, and send them the licence you get back.</div>
            <div class="grid2"><label class="field">Activation code from that computer<input type="text" id="l-code" placeholder="ABCD-EFGH-JKLM" autocomplete="off"></label>
              <label class="field">Person's name<input type="text" id="l-name" autocomplete="off"></label>
              <label class="field">Licence lasts<select id="l-days"><option value="30">30 days</option><option value="90" selected>90 days</option><option value="180">180 days</option><option value="365">1 year</option></select></label></div>
            <label class="field">Your owner key<textarea id="l-key" rows="2" placeholder="DBI-OWNER-…  (from your dericbi-owner.key file)" spellcheck="false"></textarea><span class="hint">Used once to sign, then forgotten. It is never saved on this computer.</span></label>
            <div id="l-err"></div><div><button class="btn primary" id="l-make">Create licence</button></div><div id="l-out"></div>
            <div class="small muted">A licence can't be taken back from a computer that is offline, so it simply ends on its date. Issue short ones and renew them.</div>
          </div></section>` : ''}`);
      bindPassword('a', u.username);
      const fail2 = (id, e) => render($('#' + id), html`<div class="notice bad" style="margin:0">${e.message}</div>`);
      $('#a-pform').onsubmit = async (e) => {
        e.preventDefault(); const b = $('#a-pgo'); b.disabled = true;
        try { await api('/auth/change-password', { method: 'POST', body: { current: $('#a-cur').value, new: readPassword('a') } }); toast('Password changed. Other places you were signed in are signed out.'); await draw(); }
        catch (err) { fail2('a-perr', err); b.disabled = false; }
      };
      $('#a-qform').onsubmit = async (e) => {
        e.preventDefault(); const b = $('#a-qgo'); b.disabled = true;
        try { await api('/auth/change-questions', { method: 'POST', body: { current: $('#a-qcur').value, questions: readQuestions('aq') } }); toast('Security questions saved'); await draw(); }
        catch (err) { fail2('a-qerr', err); b.disabled = false; }
      };
      if ($('#a-renew')) $('#a-renew').onclick = async () => { try { await api('/auth/renew-license', { method: 'POST', body: { license: $('#a-lic').value } }); toast('Licence updated'); App.renderAccount(box); } catch (err) { fail(err); } };
      if (u.role !== 'owner') return;
      $('#a-add').onsubmit = async (e) => {
        e.preventDefault();
        try {
          const r = await api('/users', { method: 'POST', body: { username: $('#a-newname').value } });
          await draw();
          render($('#a-temp'), html`<div class="notice" style="margin:0"><b>${r.user.username}</b> can sign in with this temporary password. It is shown only now:
            <div style="font-size:18px;margin:6px 0"><code>${r.temporary_password}</code> <button class="btn small" id="a-copytemp">Copy</button></div></div>`);
          $('#a-copytemp').onclick = () => App.copy(r.temporary_password, 'Temporary password copied');
        } catch (err) { fail(err); }
      };
      box.querySelectorAll('[data-u]').forEach((b) => (b.onclick = async () => {
        const id = b.dataset.id, what = b.dataset.u;
        try {
          if (what === 'remove') { if (!(await App.confirm('Remove this person? They will no longer be able to sign in.', 'Remove', true))) return; await api(`/users/${id}`, { method: 'DELETE' }); await draw(); }
          else if (what === 'active') { await api(`/users/${id}/active`, { method: 'POST', body: { active: b.dataset.on === '1' } }); await draw(); }
          else {
            const r = await api(`/users/${id}/reset`, { method: 'POST' }); await draw();
            render($('#a-temp'), html`<div class="notice" style="margin:0">New temporary password: <code>${r.temporary_password}</code> <button class="btn small" id="a-copytemp">Copy</button></div>`);
            $('#a-copytemp').onclick = () => App.copy(r.temporary_password, 'Temporary password copied');
          }
        } catch (err) { fail(err); }
      }));
      $('#l-make').onclick = async (e) => {
        const btn = e.currentTarget;   // read before any await: the browser clears currentTarget afterwards
        btn.disabled = true;
        try {
          const r = await api('/license/issue', { method: 'POST', body: { code: $('#l-code').value, username: $('#l-name').value, days: +$('#l-days').value, owner_key: $('#l-key').value }, timeout: 60000 });
          $('#l-key').value = '';
          render($('#l-err'), html``);
          render($('#l-out'), html`<div class="notice" style="margin:0"><b>Licence for ${r.username}, valid until ${r.expires}.</b> Send all of this text to them:
            <textarea rows="4" readonly style="margin-top:6px">${r.license}</textarea><div class="row" style="margin-top:6px"><button class="btn small" id="l-copy">Copy licence</button></div></div>`);
          $('#l-copy').onclick = () => App.copy(r.license, 'Licence copied');
        } catch (err) { fail2('l-err', err); }
        btn.disabled = false;
      };
    };
    await draw();
  };
})();
