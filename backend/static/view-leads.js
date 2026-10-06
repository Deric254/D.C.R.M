(function () {
  const { html, render, api, qs, $, $$, when, gradeChip, statusChip, toast, fail, debounce } = App;
  const defaults = () => ({ q: '', sector: '', town: '', status: '', grade: '', contact: '', archived: false, page: 1, sort: 'created_desc', selected: new Set() });
  App.state.leads = defaults();
  const PAGE = 50;

  const opt = (v, label, cur) => html`<option value="${v}" ${v === cur ? 'selected' : ''}>${label}</option>`;

  function params(S) {
    const p = { q: S.q, sector: S.sector, town: S.town, status: S.status, grade: S.grade, archived: S.archived, page: S.page, page_size: PAGE, sort: S.sort };
    if (S.contact === 'phone') p.has_phone = true;
    if (S.contact === 'email') p.has_email = true;
    if (S.contact === 'noemail') p.has_email = 'false';
    if (S.contact === 'dnc') p.dnc = true;
    return p;
  }

  App.views.leads = {
    title: 'Leads',
    async render(el, hashParams) {
      const S = App.state.leads;
      if (hashParams.status !== undefined || hashParams.grade !== undefined) {
        Object.assign(S, defaults(), { status: hashParams.status || '', grade: hashParams.grade || '' });
      }
      const f = await api('/facets');
      render(el, html`<div>
        <div class="toolbar">
          <input type="search" id="q" placeholder="Search name, phone, email, notes" value="${S.q}" aria-label="Search leads">
          <select id="f-sector" aria-label="Sector"><option value="">All sectors</option>${f.sectors.map((x) => opt(x, x, S.sector))}</select>
          <select id="f-town" aria-label="Town"><option value="">All towns</option>${f.towns.map((x) => opt(x, x, S.town))}</select>
          <select id="f-status" aria-label="Status"><option value="">Any status</option>${App.STATUSES.map((x) => opt(x, App.STATUS_LABEL[x], S.status))}</select>
          <select id="f-grade" aria-label="Grade"><option value="">Any grade</option>${['hot', 'warm', 'cold', 'unclear'].map((x) => opt(x, App.GRADE_LABEL[x], S.grade))}${opt('none', 'Not graded', S.grade)}</select>
          <select id="f-contact" aria-label="Contact details">${opt('', 'Any contact details', S.contact)}${opt('phone', 'Has a phone', S.contact)}${opt('email', 'Has an email', S.contact)}${opt('noemail', 'No email yet', S.contact)}${opt('dnc', 'Do not contact', S.contact)}</select>
          <select id="f-sort" aria-label="Sort">${opt('created_desc', 'Newest first', S.sort)}${opt('created_asc', 'Oldest first', S.sort)}${opt('name', 'Name', S.sort)}${opt('last_reply', 'Latest reply', S.sort)}${opt('grade', 'Hottest first', S.sort)}</select>
          <label class="check"><input type="checkbox" id="f-arch" ${S.archived ? 'checked' : ''}>Archived</label>
          <span class="right row">
            <button class="btn" id="btn-import">Import CSV</button>
            <button class="btn" id="btn-export">Export CSV</button>
            <button class="btn primary" id="btn-add">Add lead</button>
          </span>
          <input type="file" id="file" accept=".csv,text/csv" class="hidden">
        </div>
        <div id="bulk"></div>
        <div class="panel"><div class="tablewrap" id="table"></div></div>
        <div class="pager" id="pager"></div>
      </div>`);

      const refreshSoon = debounce(() => { S.page = 1; load(); }, 280);
      $('#q').addEventListener('input', (e) => { S.q = e.target.value; refreshSoon(); });
      const bind = (id, key, cast = (v) => v) => $(id).addEventListener('change', (e) => { S[key] = cast(e.target.type === 'checkbox' ? e.target.checked : e.target.value); S.page = 1; S.selected.clear(); load(); });
      bind('#f-sector', 'sector'); bind('#f-town', 'town'); bind('#f-status', 'status'); bind('#f-grade', 'grade');
      bind('#f-contact', 'contact'); bind('#f-sort', 'sort'); bind('#f-arch', 'archived');
      $('#btn-add').onclick = addLeadDialog;
      $('#btn-export').onclick = () => App.download('/api/leads/export' + (S.archived ? '?archived=true' : ''));
      $('#btn-import').onclick = () => $('#file').click();
      $('#file').onchange = importFile;

      $('#table').addEventListener('click', (e) => {
        const cb = e.target.closest('input[data-sel]');
        if (cb) { cb.checked ? S.selected.add(+cb.dataset.sel) : S.selected.delete(+cb.dataset.sel); cb.closest('tr').classList.toggle('sel', cb.checked); drawBulk(); return; }
        if (e.target.closest('#sel-all')) return;
        const tr = e.target.closest('tr[data-id]');
        if (tr) App.openLead(+tr.dataset.id);
      });
      $('#table').addEventListener('change', (e) => {
        if (e.target.id === 'sel-all') {
          $$('#table input[data-sel]').forEach((cb) => { cb.checked = e.target.checked; e.target.checked ? S.selected.add(+cb.dataset.sel) : S.selected.delete(+cb.dataset.sel); cb.closest('tr').classList.toggle('sel', cb.checked); });
          drawBulk();
        }
      });
      $('#pager').addEventListener('click', (e) => {
        const b = e.target.closest('button[data-page]'); if (!b) return;
        S.page = +b.dataset.page; load();
      });

      let total = 0;
      async function load() {
        let r;
        try { r = await api('/leads' + qs(params(S))); } catch (e) { return fail(e); }
        total = r.total;
        const rows = r.leads;
        render($('#table'), rows.length ? html`<table class="t">
          <thead><tr><th style="width:34px"><input type="checkbox" id="sel-all" aria-label="Select all on this page"></th><th>Business</th><th>Contact</th><th>Status</th><th>Grade</th><th>Last activity</th></tr></thead>
          <tbody>${rows.map((l) => html`<tr data-id="${l.id}" class="${S.selected.has(l.id) ? 'sel' : ''}">
            <td><input type="checkbox" data-sel="${l.id}" ${S.selected.has(l.id) ? 'checked' : ''} aria-label="Select ${l.name}"></td>
            <td><div class="name">${l.name}</div><div class="muted small">${[l.sector, l.town].filter(Boolean).join(' · ')}</div></td>
            <td><div>${l.phone_display || html`<span class="muted">No phone</span>`}</div><div class="muted small">${l.email || ''}</div></td>
            <td>${statusChip(l.status)}</td>
            <td>${gradeChip(l.grade)} ${l.do_not_contact ? html`<span class="chip optout" title="Won't be messaged">Do not contact</span>` : ''} ${l.email_bounced ? html`<span class="chip bounce">Email bounced</span>` : ''}</td>
            <td class="muted small nowrap">${l.last_reply_at ? 'Replied ' + when(l.last_reply_at) : l.last_contacted_at ? 'Messaged ' + when(l.last_contacted_at) : ''}</td>
          </tr>`)}</tbody></table>`
          : html`<div class="empty"><strong>${S.q || S.sector || S.town || S.status || S.grade || S.contact ? 'No leads match these filters' : S.archived ? 'Nothing archived' : 'No leads yet'}</strong>${S.archived ? '' : html`<a href="#/find">Find leads on Google Maps</a> or use Import CSV to bring in your list.`}</div>`);
        const pages = Math.max(1, Math.ceil(total / PAGE));
        render($('#pager'), html`<span>${total} lead${total === 1 ? '' : 's'}</span>
          <button class="btn small" data-page="${S.page - 1}" ${S.page <= 1 ? 'disabled' : ''}>Previous</button>
          <span>Page ${S.page} of ${pages}</span>
          <button class="btn small" data-page="${S.page + 1}" ${S.page >= pages ? 'disabled' : ''}>Next</button>`);
        drawBulk();
      }

      function drawBulk() {
        const n = S.selected.size, box = $('#bulk');
        if (!n) return render(box, html``);
        render(box, html`<div class="bulkbar"><strong>${n} selected</strong>
          <select id="b-status"><option value="">Set status…</option>${App.STATUSES.map((x) => opt(x, App.STATUS_LABEL[x], ''))}</select>
          <select id="b-grade"><option value="">Set grade…</option>${['hot', 'warm', 'cold', 'unclear'].map((x) => opt(x, App.GRADE_LABEL[x], ''))}${opt('-', 'Clear grade', '')}</select>
          <button class="btn small" id="b-dnc">Do not contact</button>
          <button class="btn small" id="b-arch">${S.archived ? 'Restore' : 'Archive'}</button>
          <button class="btn small quiet right" id="b-clear" style="color:inherit">Clear selection</button></div>`);
        const run = async (body, msg) => {
          try { await api('/leads/bulk', { method: 'POST', body: { ids: [...S.selected], ...body } }); toast(msg); S.selected.clear(); load(); }
          catch (e) { fail(e); }
        };
        $('#b-status').onchange = (e) => e.target.value && run({ action: 'status', value: e.target.value }, 'Status updated');
        $('#b-grade').onchange = (e) => e.target.value && run({ action: 'grade', value: e.target.value === '-' ? '' : e.target.value }, 'Grade updated');
        $('#b-dnc').onclick = async () => { if (await App.confirm(`Mark ${n} lead(s) as do-not-contact? They will never be messaged.`, 'Mark do-not-contact', true)) run({ action: 'dnc', value: '1' }, 'Marked do-not-contact'); };
        $('#b-arch').onclick = () => run({ action: S.archived ? 'restore' : 'archive' }, S.archived ? 'Restored' : "Archived (kept on record so it can't come back as a duplicate)");
        $('#b-clear').onclick = () => { S.selected.clear(); load(); };
      }

      async function importFile(e) {
        const file = e.target.files[0]; if (!file) return;
        try {
          const r = await api('/leads/import', { method: 'POST', body: { csv: await file.text() } });
          await App.dialog({ title: 'Import finished', ok: 'Done', cancel: '', body: html`
            <p><strong>${r.created}</strong> new lead${r.created === 1 ? '' : 's'} added.</p>
            <p>${r.duplicates} already in your CRM (skipped)${r.invalid ? `, ${r.invalid} rows had no business name` : ''}.</p>
            ${r.samples.length ? html`<div class="muted small">${r.samples.map((x) => html`<div>${x}</div>`)}</div>` : ''}` });
          load();
        } catch (err) { fail(err); }
        e.target.value = '';
      }

      App.refreshLeadsTable = load;
      await load();
      return () => { App.refreshLeadsTable = null; };
    },
  };

  function addLeadDialog() {
    App.formDialog({
      title: 'Add a lead', ok: 'Add lead',
      body: html`<div class="grid2">
        <label class="field">Business name<input type="text" name="name" required autofocus></label>
        <label class="field">Sector<input type="text" name="sector" placeholder="Pharmacy, Agrovet…"></label>
        <label class="field">Town<input type="text" name="town"></label>
        <label class="field">Phone<input type="text" name="phone" placeholder="0712 345 678"></label>
        <label class="field">Email<input type="email" name="email"></label>
        <label class="field">Website<input type="text" name="website"></label></div>
        <label class="field">Address<input type="text" name="address"></label>
        <label class="field">Notes<textarea name="notes" rows="2"></textarea></label>`,
    }, async (form) => {
      try {
        const lead = await api('/leads', { method: 'POST', body: Object.fromEntries(new FormData(form)) });
        toast('Lead added');
        if (App.refreshLeadsTable) App.refreshLeadsTable();
        return lead;
      } catch (e) {
        if (e.status === 409 && e.data && e.data.existing_id) {
          // Already in the CRM: close the form and show the lead that's already there.
          toast(e.message, true);
          setTimeout(() => App.openLead(e.data.existing_id), 60);
          return null;
        }
        throw e;
      }
    });
  }

  // ================================================================= drawer
  const TAGS = ['{name}', '{town}', '{sector}', '{sender}'];
  async function getSettings() { return (App.state.settings = await api('/settings')); }

  App.openLead = async function openLead(id) {
    const ov = $('#overlay');
    let lead;
    try { lead = await api('/leads/' + id); } catch (e) { return fail(e); }
    const close = () => { render(ov, html``); document.removeEventListener('keydown', onKey); };
    const onKey = (e) => { if (e.key === 'Escape' && !$('#dlg').open) close(); };
    document.addEventListener('keydown', onKey);

    const items = [];
    lead.messages.forEach((m) => items.push({ t: m.sent_at || m.created_at, m }));
    lead.events.filter((e) => !['reply', 'sent'].includes(e.kind)).forEach((e) => items.push({ t: e.ts, e }));
    items.sort((a, b) => (a.t < b.t ? -1 : a.t > b.t ? 1 : 0));

    const timeline = items.length ? items.map(({ t, m, e }) => m ? (m.direction === 'in' ? html`
      <div class="tl in"><i></i><div><div class="small muted">${m.channel === 'email' ? 'Replied by email' : m.channel === 'sms' ? 'Replied by SMS' : 'Reply noted'} · ${when(t)} ${gradeChip(m.grade)}</div>
        <div class="bubble">${m.subject ? html`<strong>${m.subject}</strong><br>` : ''}${m.body}</div>
        ${m.reasons ? html`<div class="small muted">Matched: ${m.reasons}</div>` : ''}</div></div>` : html`
      <div class="tl out"><i></i><div><div class="small muted">${m.channel === 'email' ? 'Emailed' : 'Texted'}${m.campaign_name ? ' (' + m.campaign_name + ')' : ''} · ${when(t)} · ${m.status === 'sent' ? 'Sent' : m.status === 'queued' ? 'Waiting to send' : m.status === 'failed' ? 'Failed' : m.status}</div>
        <div class="bubble">${m.subject ? html`<strong>${m.subject}</strong><br>` : ''}${m.body}</div>
        ${m.error ? html`<div class="small" style="color:var(--bad)">${m.error}</div>` : ''}</div></div>`)
      : html`<div class="tl"><i></i><div><div class="small muted">${when(t)}</div><div>${e.detail}</div></div></div>`)
      : html`<div class="muted">No history yet.</div>`;

    const canEmail = lead.email && !lead.email_bounced && !lead.do_not_contact;
    const canSms = lead.phone_norm && lead.is_mobile && !lead.do_not_contact;

    render(ov, html`<div class="scrim" id="scrim"></div>
      <aside class="drawer" role="dialog" aria-label="${lead.name}">
        <div class="drawer-head">
          <div style="flex:1;min-width:0"><h2>${lead.name}</h2>
            <div class="row" style="gap:6px;margin-top:6px">${statusChip(lead.status)} ${gradeChip(lead.grade)}
              ${lead.do_not_contact ? html`<span class="chip optout">Do not contact</span>` : ''}${lead.email_bounced ? html`<span class="chip bounce">Email bounced</span>` : ''}
              ${lead.archived ? html`<span class="chip">Archived</span>` : ''}</div></div>
          <button class="btn quiet" id="x" aria-label="Close">Close</button>
        </div>
        <div class="drawer-body">
          <div class="row">
            ${lead.phone_norm ? html`<button class="btn small" type="button" id="copy-phone" title="Copy the number to dial it">Copy ${lead.phone_display}</button>` : ''}
            <button class="btn small" data-compose="email" ${canEmail ? '' : 'disabled'} title="${canEmail ? '' : 'Needs a working email and not do-not-contact'}">Email</button>
            <button class="btn small" data-compose="sms" ${canSms ? '' : 'disabled'} title="${canSms ? '' : 'Needs a mobile number and not do-not-contact'}">Text</button>
            <button class="btn small" id="log-reply">Log a reply</button>
            ${lead.messages.length ? html`<button class="btn small" id="ai-summary" title="A short summary and the best next step">Summarise with AI</button>` : ''}
            ${lead.maps_url ? html`<a class="btn small" href="${lead.maps_url}" rel="noopener noreferrer">Google Maps</a>` : ''}
            ${/^https?:\/\//i.test(lead.website) ? html`<a class="btn small" href="${lead.website}" rel="noopener noreferrer">Website</a>` : ''}
          </div>
          <div id="composer"></div>

          <form id="edit" class="stack" style="gap:12px">
            <div class="grid2">
              <label class="field">Status<select name="status">${App.STATUSES.map((x) => opt(x, App.STATUS_LABEL[x], lead.status))}</select></label>
              <label class="field">Grade<select name="grade">${opt('', 'Automatic', lead.grade_locked ? lead.grade : '')}${['hot', 'warm', 'cold', 'unclear'].map((x) => opt(x, App.GRADE_LABEL[x], lead.grade_locked ? lead.grade : '__'))}</select>
                <span class="hint">${lead.grade_locked ? "Set by you. Replies won't change it." : lead.grade ? 'Set from their replies.' : 'Graded when they reply.'}</span></label>
              <label class="field">Business name<input type="text" name="name" value="${lead.name}" required></label>
              <label class="field">Follow up on<input type="date" name="next_followup" value="${lead.next_followup}"></label>
              <label class="field">Sector<input type="text" name="sector" value="${lead.sector}"></label>
              <label class="field">Town<input type="text" name="town" value="${lead.town}"></label>
              <label class="field">Phone<input type="text" name="phone" value="${lead.phone}"></label>
              <label class="field">Email<input type="email" name="email" value="${lead.email || ''}"></label>
            </div>
            <label class="field">Address<input type="text" name="address" value="${lead.address}"></label>
            <label class="field">Website<input type="text" name="website" value="${lead.website}"></label>
            <label class="field">Notes<textarea name="notes" rows="3">${lead.notes}</textarea></label>
            <label class="check"><input type="checkbox" name="do_not_contact" ${lead.do_not_contact ? 'checked' : ''}>Do not contact (blocks all messages to this lead)</label>
            <div class="row"><button class="btn primary" type="submit">Save changes</button>
              <button class="btn right ${lead.archived ? '' : 'danger'}" type="button" id="archive">${lead.archived ? 'Restore lead' : 'Archive lead'}</button></div>
          </form>

          <div><h3 style="margin-bottom:10px">History</h3><div class="timeline">${timeline}</div></div>
        </div>
      </aside>`);

    $('#scrim').onclick = close; $('#x').onclick = close;
    if ($('#copy-phone')) $('#copy-phone').onclick = () => App.copy(lead.phone_norm, 'Number copied');
    const reload = () => { close(); App.openLead(id); if (App.refreshLeadsTable) App.refreshLeadsTable(); if (location.hash.startsWith('#/overview') || location.hash.startsWith('#/replies')) App.refresh(); };

    $('#edit').addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = Object.fromEntries(new FormData(e.target));
      fd.do_not_contact = e.target.elements.do_not_contact.checked;
      try { await api('/leads/' + id, { method: 'PATCH', body: fd }); toast('Saved'); reload(); } catch (err) { fail(err); }
    });
    $('#archive').onclick = async () => {
      try { await api('/leads/bulk', { method: 'POST', body: { ids: [id], action: lead.archived ? 'restore' : 'archive' } });
        toast(lead.archived ? 'Restored' : 'Archived'); close(); if (App.refreshLeadsTable) App.refreshLeadsTable(); } catch (err) { fail(err); }
    };

    // ---- composer: send message / log reply
    $$('[data-compose]', ov).forEach((b) => (b.onclick = () => composer(b.dataset.compose)));
    $('#log-reply').onclick = () => logReply();
    if ($('#ai-summary')) $('#ai-summary').onclick = async (e) => {
      const b = e.currentTarget; b.disabled = true;
      try {
        const r = await api(`/leads/${id}/ai-summary`, { method: 'POST', timeout: 100000 });
        render($('#composer'), html`<div class="panel"><div class="panel-body stack" style="gap:8px"><h3>Summary</h3><div class="sample">${r.summary}</div>
          <div><button class="btn quiet small" type="button" id="cancel-c">Close</button></div></div></div>`);
        $('#cancel-c').onclick = () => render($('#composer'), html``);
      } catch (err) { fail(err); } finally { b.disabled = false; }
    };

    async function composer(channel) {
      const s = App.state.settings || (await getSettings());
      const ready = channel === 'email' ? s.smtp_host && s.from_email : s.at_username && s.at_api_key_set;
      render($('#composer'), html`<form class="panel" id="send-form"><div class="panel-body stack" style="gap:10px">
        <h3>${channel === 'email' ? 'Email ' : 'Text '}${lead.name}</h3>
        ${!ready ? html`<div class="notice">${channel === 'email' ? 'Email' : 'SMS'} isn't set up yet. <a href="#/settings" id="goset">Open settings</a></div>` : ''}
        ${channel === 'email' ? html`<label class="field">Subject<input type="text" name="subject" required></label>` : ''}
        <label class="field">Message<textarea name="body" rows="5" required></textarea></label>
        <div class="row tags"><button type="button" class="btn small primary" id="ai-write" title="Writes a first message or a reply to their latest one. Uses anything you have typed as guidance.">Write with AI</button>${TAGS.map((t) => html`<button type="button" class="btn small" data-tag="${t}">${t}</button>`)}
          <span class="smsmeter right" id="meter"></span></div>
        <div class="small muted">${s.append_optout ? 'An opt-out line is added automatically.' : ''}</div>
        <div class="row"><button class="btn primary" type="submit" ${ready ? '' : 'disabled'}>${channel === 'email' ? 'Send email' : 'Send text'}</button>
          <button class="btn quiet" type="button" id="cancel-c">Cancel</button></div></div></form>`);
      const f = $('#send-form'); const ta = f.elements.body;
      const meter = () => { if (channel !== 'sms') return; const len = ta.value.length + (s.append_optout ? (s.optout_suffix_sms || '').length : 0); $('#meter').textContent = `${len} characters · ${len <= 160 ? 1 : Math.ceil(len / 153)} SMS`; };
      ta.addEventListener('input', meter); meter();
      $$('[data-tag]', f).forEach((b) => (b.onclick = () => { const p = ta.selectionStart; ta.setRangeText(b.dataset.tag, p, ta.selectionEnd, 'end'); ta.focus(); meter(); }));
      $('#cancel-c').onclick = () => render($('#composer'), html``);
      $('#ai-write').onclick = (e) => App.aiFill(e.currentTarget, `/leads/${id}/ai-draft`, channel, f);
      $('#goset') && ($('#goset').onclick = close);
      f.addEventListener('submit', async (e) => {
        e.preventDefault(); const btn = e.submitter; btn.disabled = true;
        try { await api(`/leads/${id}/send`, { method: 'POST', body: { channel, subject: f.elements.subject ? f.elements.subject.value : '', body: ta.value } });
          toast(channel === 'email' ? 'Email sent' : 'Text sent'); reload(); }
        catch (err) { fail(err); btn.disabled = false; }
      });
      ta.focus();
    }

    function logReply() {
      render($('#composer'), html`<form class="panel" id="reply-form"><div class="panel-body stack" style="gap:10px">
        <h3>Log a reply from ${lead.name}</h3>
        <p class="muted small">Paste what they said by SMS, WhatsApp or on a call. It's graded automatically so it shows up in Replies.</p>
        <div class="grid2"><label class="field">How did they reply?<select name="channel"><option value="sms">SMS</option><option value="email">Email</option><option value="other">WhatsApp or call</option></select></label>
          <label class="field">Grade<select name="grade"><option value="">Grade it for me</option><option value="hot">Hot</option><option value="warm">Warm</option><option value="cold">Cold</option><option value="unclear">Needs a look</option><option value="optout">Opted out</option></select></label></div>
        <label class="field">What did they say?<textarea name="text" rows="3" required></textarea></label>
        <div class="row"><button class="btn primary" type="submit">Save reply</button><button class="btn quiet" type="button" id="cancel-c">Cancel</button></div></div></form>`);
      $('#cancel-c').onclick = () => render($('#composer'), html``);
      $('#reply-form').addEventListener('submit', async (e) => {
        e.preventDefault(); const fd = Object.fromEntries(new FormData(e.target));
        try { const r = await api(`/leads/${id}/reply`, { method: 'POST', body: { text: fd.text, channel: fd.channel, grade: fd.grade || null } });
          toast(`Graded ${App.GRADE_LABEL[r.grade] || r.grade}${r.reasons && r.reasons.length ? ' (matched: ' + r.reasons.join(', ') + ')' : ''}`); reload(); }
        catch (err) { fail(err); }
      });
      $('#reply-form').elements.text.focus();
    }
  };
})();
