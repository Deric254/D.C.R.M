(function () {
  const { html, render, api, qs, $, $$, when, gradeChip, toast, fail, debounce } = App;
  const TABS = [['review', 'To review'], ['hot', 'Hot'], ['warm', 'Warm'], ['cold', 'Cold'], ['optout', 'Opted out'], ['all', 'Everything']];
  const GRADES = [['hot', 'Hot'], ['warm', 'Warm'], ['cold', 'Cold'], ['unclear', 'Needs a look'], ['optout', 'Opted out']];
  let tab = 'review';

  App.views.replies = {
    title: 'Replies',
    async render(el) {
      render(el, html`<div class="stack">
        <div class="row">
          <p class="muted" id="polled"></p>
          <span class="right row"><button class="btn" id="check">Check email now</button><button class="btn primary" id="log">Log a reply</button></span>
        </div>
        <div class="tabs" id="tabs">${TABS.map(([k, l]) => html`<button class="tab ${k === tab ? 'active' : ''}" data-tab="${k}">${l}</button>`)}</div>
        <div class="stack" id="list" style="gap:10px"></div></div>`);

      async function status() {
        const s = await api('/status').catch(() => null); if (!s) return;
        const p = s.poller || {};
        render($('#polled'), html`${s.replies_ready ? (p.at ? html`Email inbox last checked ${when(p.at)}${p.ok === false ? html` · <span style="color:var(--bad)">${p.message}</span>` : ''}.` : 'Email inbox will be checked shortly.')
          : html`Email replies aren't tracked yet. <a href="#/settings">Connect your reply inbox</a> to see them here. SMS replies can be added with Log a reply.`}`);
      }

      async function load() {
        const q = {};
        if (tab === 'review') q.handled = 'false';
        else if (tab !== 'all') q.grade = tab;
        let rows; try { rows = await api('/inbox' + qs(q)); } catch (e) { return fail(e); }
        if (tab === 'review') rows = rows.filter((r) => ['hot', 'warm', 'unclear'].includes(r.grade));
        render($('#list'), rows.length ? rows.map((r) => html`
          <article class="reply ${r.handled ? 'done' : ''}"><div class="rail ${r.grade}"></div><div class="reply-body">
            <div class="row"><a href="#" data-lead="${r.lead_id}" class="name" style="font-weight:650;color:inherit">${r.name}</a>
              <span class="muted small">${[r.town, r.sector].filter(Boolean).join(' · ')}</span>
              ${gradeChip(r.grade)} ${r.do_not_contact ? html`<span class="chip optout">Do not contact</span>` : ''}
              <span class="muted small right">${r.channel === 'email' ? 'Email' : r.channel === 'sms' ? 'SMS' : 'Noted'} · ${when(r.created_at)}${r.campaign_name ? ' · ' + r.campaign_name : ''}</span></div>
            ${r.subject ? html`<div class="small muted">${r.subject}</div>` : ''}
            <div class="quote">${r.body || '(empty message)'}</div>
            <div class="row">
              <label class="row small muted" style="gap:6px">Grade <select data-regrade="${r.id}" style="width:auto">${GRADES.map(([v, l]) => html`<option value="${v}" ${v === r.grade ? 'selected' : ''}>${l}</option>`)}${['auto', 'bounce'].includes(r.grade) ? html`<option selected>${App.GRADE_LABEL[r.grade]}</option>` : ''}</select></label>
              ${r.reasons ? html`<span class="small muted">Matched: ${r.reasons}</span>` : ''}
              <span class="right row"><button class="btn small" data-lead="${r.lead_id}">Open lead</button>
                <button class="btn small ${r.handled ? '' : 'primary'}" data-handled="${r.id}" data-to="${r.handled ? 0 : 1}">${r.handled ? 'Move back to review' : 'Mark handled'}</button></span></div>
          </div></article>`)
          : html`<div class="panel"><div class="empty"><strong>${tab === 'review' ? 'Nothing waiting for you' : 'No replies here'}</strong>${tab === 'review' ? 'Replies that need a human look appear here, hottest first in the Hot tab.' : ''}</div></div>`);
      }

      el.addEventListener('click', async (e) => {
        const t = e.target.closest('[data-tab]');
        if (t) { tab = t.dataset.tab; $$('#tabs .tab').forEach((b) => b.classList.toggle('active', b === t)); return load(); }
        const lead = e.target.closest('[data-lead]');
        if (lead) { e.preventDefault(); return App.openLead(+lead.dataset.lead); }
        const h = e.target.closest('[data-handled]');
        if (h) { try { await api(`/messages/${h.dataset.handled}/handled`, { method: 'POST', body: { handled: h.dataset.to === '1' } }); load(); App.pollStatus(); } catch (err) { fail(err); } }
      });
      el.addEventListener('change', async (e) => {
        const s = e.target.closest('[data-regrade]'); if (!s) return;
        try { await api(`/messages/${s.dataset.regrade}/regrade`, { method: 'POST', body: { grade: s.value } }); toast('Grade updated'); load(); App.pollStatus(); } catch (err) { fail(err); }
      });
      $('#check').onclick = async (e) => {
        e.target.disabled = true;
        try { const r = await api('/inbox/check', { method: 'POST' }); toast(r.fetched ? `${r.replies} new repl${r.replies === 1 ? 'y' : 'ies'}, ${r.bounces} bounce${r.bounces === 1 ? '' : 's'}` : 'No new mail'); load(); status(); App.pollStatus(); }
        catch (err) { fail(err); } finally { e.target.disabled = false; }
      };
      $('#log').onclick = logReply;

      function logReply() {
        let chosen = null;
        const p = App.formDialog({ title: 'Log a reply', ok: 'Save reply', body: html`
          <p class="muted small">For replies that arrive outside the app: an SMS on your phone, WhatsApp, or a call. It is graded like any other reply.</p>
          <label class="field">Find the business<input type="search" id="lead-q" placeholder="Type a name or phone number" autocomplete="off" autofocus></label>
          <div id="lead-res" class="stack" style="gap:4px"></div>
          <div class="grid2"><label class="field">How did they reply?<select name="channel"><option value="sms">SMS</option><option value="email">Email</option><option value="other">WhatsApp or call</option></select></label>
            <label class="field">Grade<select name="grade"><option value="">Grade it for me</option>${GRADES.map(([v, l]) => html`<option value="${v}">${l}</option>`)}</select></label></div>
          <label class="field">What did they say?<textarea name="text" rows="3" required></textarea></label>` },
        async (form) => {
          if (!chosen) throw new Error('Pick the business first');
          const fd = Object.fromEntries(new FormData(form));
          const r = await api(`/leads/${chosen}/reply`, { method: 'POST', body: { text: fd.text, channel: fd.channel, grade: fd.grade || null } });
          toast(`Graded ${App.GRADE_LABEL[r.grade] || r.grade}${r.reasons && r.reasons.length ? ' (matched: ' + r.reasons.join(', ') + ')' : ''}`);
          load(); App.pollStatus();
        });
        const search = debounce(async () => {
          const v = $('#lead-q').value.trim(); if (!v) return render($('#lead-res'), html``);
          const r = await api('/leads' + qs({ q: v, page_size: 6 })).catch(() => ({ leads: [] }));
          render($('#lead-res'), r.leads.length ? html`${r.leads.map((l) => html`<label class="opt" style="border-radius:var(--r)"><input type="radio" name="pick" value="${l.id}"><span>${l.name} <span class="muted small">${[l.town, l.phone_display].filter(Boolean).join(' · ')}</span></span></label>`)}`
            : html`<div class="muted small">No match. Add the lead first from the Leads page.</div>`);
        }, 250);
        $('#lead-q').addEventListener('input', search);
        $('#lead-res').addEventListener('change', (e) => { chosen = +e.target.value; });
        return p;
      }

      status(); await load();
      const t = setInterval(() => { status(); if (!$('#dlg').open) load(); }, 15000);
      return () => clearInterval(t);
    },
  };
})();
