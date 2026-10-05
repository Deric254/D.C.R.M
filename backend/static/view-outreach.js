(function () {
  const { html, render, api, $, $$, when, toast, fail, debounce } = App;
  const TAGS = ['{name}', '{town}', '{sector}', '{sender}'];
  const CSTATUS = { draft: ['Draft', ''], running: ['Sending', 'ok'], paused: ['Paused', 'warm'], done: ['Done', 'ok'], cancelled: ['Cancelled', 'cold'] };

  App.views.outreach = {
    title: 'Outreach',
    async render(el) {
      const [facets, settings, list] = await Promise.all([api('/facets'), api('/settings'), api('/campaigns')]);
      let showForm = list.length === 0;
      let timer;

      function formHtml() {
        const chk = (name, vals, sel) => vals.map((v) => html`<label class="opt"><input type="checkbox" name="${name}" value="${v}" ${sel.includes(v) ? 'checked' : ''}>${App.STATUS_LABEL[v] || v}</label>`);
        return html`<section class="panel" id="newpanel"><div class="panel-head"><h2>New campaign</h2><button class="btn small quiet right" id="hide-form">Close</button></div>
          <form id="cform" class="panel-body stack" style="gap:16px">
            <div class="grid2">
              <label class="field">Name <span class="hint">Only for you, e.g. “Meru pharmacies, October”</span><input type="text" name="name"></label>
              <div class="field">How to reach them
                <div class="chips" style="margin-top:2px"><label class="opt"><input type="radio" name="channel" value="sms" checked>Text message</label>
                  <label class="opt"><input type="radio" name="channel" value="email">Email</label></div></div>
            </div>
            <label class="field hidden" id="subj-wrap">Subject<input type="text" name="subject" placeholder="A quick idea for {name}"></label>
            <div class="field">Message
              <textarea name="body" rows="6" placeholder="Hi {name}, I'm Deric from DericBI. We help pharmacies in {town} see stock, sales and expiry in one dashboard. Want a 10-minute demo?"></textarea>
              <div class="row tags">${TAGS.map((t) => html`<button type="button" class="btn small" data-tag="${t}">${t}</button>`)}<span class="smsmeter right" id="meter"></span></div>
              <span class="hint">${settings.append_optout ? 'An opt-out line is added to every message, and anyone who replies STOP is never contacted again.' : ''}</span></div>

            <div class="stack" style="gap:12px"><h3>Who gets it</h3>
              <div><div class="small muted" style="margin-bottom:4px">Sector</div><div class="chips">${facets.sectors.length ? chk('sectors', facets.sectors, []) : html`<span class="muted">No sectors yet</span>`}</div></div>
              <div><div class="small muted" style="margin-bottom:4px">Town</div><div class="chips">${facets.towns.length ? chk('towns', facets.towns, []) : html`<span class="muted">No towns yet</span>`}</div></div>
              <div><div class="small muted" style="margin-bottom:4px">Lead status</div><div class="chips">${chk('statuses', App.STATUSES, ['new'])}</div></div>
              <div class="grid3">
                <label class="field">Max recipients<input type="number" name="limit" min="1" placeholder="Everyone who matches"></label>
                <label class="field">Skip anyone messaged in the last (days)<input type="number" name="skip" min="0" value="${settings.skip_recent_days}"></label>
                <label class="check" style="align-self:end;padding-bottom:8px"><input type="checkbox" name="exclude_replied" checked>Skip leads who already replied</label>
              </div>
            </div>
            <div id="preview" class="stack" style="gap:8px"></div>
            <div class="row"><button class="btn primary" type="button" id="go" disabled>Start sending</button><button class="btn" type="button" id="draft">Save as draft</button></div>
          </form></section>`;
      }

      render(el, html`<div class="stack">
        <div class="row"><p class="muted">Messages go out one at a time within your daily limits and sending hours. Leads marked do-not-contact, bounced emails and anyone contacted recently are always skipped.</p>
          <button class="btn primary right ${showForm ? 'hidden' : ''}" id="new">New campaign</button></div>
        <div id="formwrap">${showForm ? formHtml() : ''}</div>
        <section class="panel"><div class="panel-head"><h2>Campaigns</h2></div><div id="clist"></div></section>
      </div>`);

      // ---------------------------------------------------------------- list
      async function drawList() {
        let rows; try { rows = await api('/campaigns'); } catch (_) { return; }
        render($('#clist'), rows.length ? html`<div class="tablewrap"><table class="t"><thead><tr><th>Campaign</th><th>Status</th><th style="width:170px">Progress</th><th>Failed</th><th>Replies</th><th></th></tr></thead><tbody>
          ${rows.map((c) => { const [lab, cls] = CSTATUS[c.status] || [c.status, '']; return html`<tr style="cursor:default">
            <td><div class="name">${c.name}</div><div class="muted small">${c.channel === 'sms' ? 'Text' : 'Email'} · ${c.launched_at ? 'Started ' + when(c.launched_at) : 'Created ' + when(c.created_at)}</div>${c.last_error ? html`<div class="small" style="color:var(--bad)">${c.last_error}</div>` : ''}</td>
            <td><span class="chip ${cls}">${lab}</span></td>
            <td><div class="progress"><i style="width:${App.pct(c.sent, c.total)}%"></i></div><div class="small muted">${c.sent} of ${c.total} sent${c.queued ? ` · ${c.queued} waiting` : ''}</div></td>
            <td>${c.failed || '0'}${c.skipped ? html`<div class="small muted">${c.skipped} skipped</div>` : ''}</td>
            <td>${c.replies}${c.hot ? html` <span class="chip hot">${c.hot} hot</span>` : ''}</td>
            <td class="nowrap">${c.status === 'running' ? html`<button class="btn small" data-act="pause" data-id="${c.id}">Pause</button>` : ''}
              ${c.status === 'paused' ? html`<button class="btn small primary" data-act="resume" data-id="${c.id}">Resume</button>` : ''}
              ${c.status === 'draft' ? html`<button class="btn small primary" data-act="launch" data-id="${c.id}">Start sending</button>` : ''}
              ${c.failed && c.status !== 'cancelled' ? html`<button class="btn small" data-act="retry" data-id="${c.id}" title="Try failed messages again">Retry failed</button>` : ''}
              ${['running', 'paused', 'draft'].includes(c.status) ? html`<button class="btn small danger" data-act="cancel" data-id="${c.id}">Cancel</button>` : ''}
              <button class="btn small quiet" data-act="view" data-id="${c.id}">Details</button></td></tr>`; })}
          </tbody></table></div>` : html`<div class="empty"><strong>No campaigns yet</strong>Write a message above and choose who should get it.</div>`);
      }
      await drawList();
      timer = setInterval(drawList, 5000);

      $('#clist').addEventListener('click', async (e) => {
        const b = e.target.closest('button[data-act]'); if (!b) return;
        const id = +b.dataset.id, act = b.dataset.act;
        try {
          if (act === 'view') return showDetails(id);
          if (act === 'cancel' && !(await App.confirm('Cancel this campaign? Messages that have not been sent yet will be dropped.', 'Cancel campaign', true))) return;
          if (act === 'launch' && !(await App.confirm('Start sending this campaign now?', 'Start sending'))) return;
          await api(`/campaigns/${id}/${act}`, { method: 'POST' });
          toast({ pause: 'Paused', resume: 'Resumed', cancel: 'Cancelled', launch: 'Sending started', retry: 'Failed messages queued again' }[act]);
          drawList(); App.pollStatus();
        } catch (err) { fail(err); }
      });

      async function showDetails(id) {
        const c = await api('/campaigns/' + id);
        const first = c.messages;
        App.dialog({ title: c.name, ok: 'Close', cancel: '', wide: true, body: html`
          <div class="muted small">${c.channel === 'sms' ? 'Text' : 'Email'} · ${c.sent} sent · ${c.queued} waiting · ${c.failed} failed · ${c.replies} replies (${c.hot} hot, ${c.warm} warm)</div>
          <div class="sample">${c.channel === 'email' ? c.subject + '\n\n' : ''}${c.body}</div>
          <div class="tablewrap" style="max-height:300px"><table class="t"><thead><tr><th>Business</th><th>To</th><th>Status</th><th>When</th></tr></thead><tbody>
          ${first.map((m) => html`<tr style="cursor:default"><td>${m.name}</td><td class="small">${m.to_addr}</td>
            <td>${m.status === 'sent' ? html`<span class="chip ok">Sent</span>` : m.status === 'failed' ? html`<span class="chip bounce" title="${m.error}">Failed</span>` : m.status === 'skipped' ? html`<span class="chip cold" title="${m.error}">Skipped</span>` : html`<span class="chip">Waiting</span>`}
              ${m.error ? html`<div class="small muted">${m.error}</div>` : ''}</td><td class="small muted">${m.sent_at ? when(m.sent_at) : ''}</td></tr>`)}</tbody></table></div>` });
      }

      // ---------------------------------------------------------------- form
      $('#new').onclick = () => { $('#formwrap').innerHTML = formHtml().s; $('#new').classList.add('hidden'); bindForm(); };
      if (showForm) bindForm();

      function bindForm() {
        const f = $('#cform'); if (!f) return;
        const getBody = () => {
          const fd = new FormData(f);
          const filters = { sectors: fd.getAll('sectors'), towns: fd.getAll('towns'), statuses: fd.getAll('statuses'), exclude_replied: f.elements.exclude_replied.checked, skip_recent_days: +fd.get('skip') || 0 };
          if (+fd.get('limit') > 0) filters.limit = +fd.get('limit');
          return { name: fd.get('name'), channel: fd.get('channel'), subject: fd.get('subject') || '', body: fd.get('body'), filters };
        };
        let lastPreview = null;
        const meter = () => {
          const ch = f.elements.channel.value; $('#subj-wrap').classList.toggle('hidden', ch !== 'email');
          if (ch !== 'sms') return render($('#meter'), html``);
          const len = f.elements.body.value.length + (settings.append_optout ? (settings.optout_suffix_sms || '').length : 0);
          $('#meter').textContent = `${len} characters · ${len <= 160 ? 1 : Math.ceil(len / 153)} SMS each`;
        };
        const preview = debounce(async () => {
          if (!document.body.contains(f)) return;   // user already left this page
          meter(); const body = getBody(); const box = $('#preview'); $('#go').disabled = true; lastPreview = null;
          if (!body.body.trim()) return render(box, html`<div class="muted">Write your message to see who it will reach.</div>`);
          try {
            const p = await api('/campaigns/preview', { method: 'POST', body });
            if (!document.body.contains(f)) return;
            lastPreview = p;
            const cap = body.channel === 'sms' ? settings.sms_daily_cap : settings.email_daily_cap;
            const days = p.will_send ? Math.ceil(p.will_send / Math.max(1, cap)) : 0;
            render(box, html`
              ${!p.channel_ready ? html`<div class="notice bad">${body.channel === 'sms' ? 'SMS' : 'Email'} isn't connected yet. <a href="#/settings">Open settings</a> before sending.</div>` : ''}
              <div><strong>${p.will_send}</strong> lead${p.will_send === 1 ? '' : 's'} will get this${p.matching > p.will_send ? ` (${p.matching} match, limited to ${p.will_send})` : ''}.
                ${days > 1 ? html`<span class="muted">At your daily limit of ${cap} that takes about ${days} days.</span>` : ''}</div>
              ${p.sample ? html`<div class="small muted">Example for ${p.sample.lead}, sent to ${p.sample.to}</div>
                <div class="sample">${p.sample.subject ? p.sample.subject + '\n\n' : ''}${p.sample.body}</div>
                ${p.sample.segments > 1 ? html`<div class="small muted">That's ${p.sample.segments} SMS per lead.</div>` : ''}`
                : html`<div class="notice">Nobody matches yet. Check the sector, town and status filters.</div>`}`);
            $('#go').disabled = !p.will_send || !p.channel_ready;
          } catch (err) { render(box, html`<div class="notice">${err.message}</div>`); }
        }, 400);
        f.addEventListener('input', preview); f.addEventListener('change', preview);
        $$('[data-tag]', f).forEach((b) => (b.onclick = () => { const ta = f.elements.body; ta.setRangeText(b.dataset.tag, ta.selectionStart, ta.selectionEnd, 'end'); ta.focus(); preview(); }));
        $('#hide-form').onclick = () => { render($('#formwrap'), html``); $('#new').classList.remove('hidden'); };
        const submit = async (launch) => {
          try {
            if (launch && !(await App.confirm(`Start sending to ${lastPreview.will_send} lead${lastPreview.will_send === 1 ? '' : 's'}? Messages go out gradually within your limits.`, 'Start sending'))) return;
            await api('/campaigns', { method: 'POST', body: { ...getBody(), launch } });
            toast(launch ? 'Sending started' : 'Saved as draft');
            render($('#formwrap'), html``); $('#new').classList.remove('hidden'); drawList(); App.pollStatus();
          } catch (err) { fail(err); }
        };
        $('#go').onclick = () => submit(true);
        $('#draft').onclick = () => submit(false);
        preview();
      }
      return () => clearInterval(timer);
    },
  };
})();
