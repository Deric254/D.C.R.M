(function () {
  const { html, render, api, $, $$, when, toast, fail, debounce } = App;
  const TAGS = ['{name}', '{town}', '{pain}', '{website}', '{whatsapp}'];
  const CHLAB = { email: 'Email', sms: 'Text', whatsapp: 'WhatsApp' };
  const CSTATUS = { draft: ['Draft', ''], running: ['Sending', 'ok'], paused: ['Paused', 'warm'], done: ['Done', 'ok'], cancelled: ['Cancelled', 'cold'], scheduled: ['Waiting for follow-up time', 'warm'] };

  App.views.outreach = {
    title: 'Outreach',
    async render(el) {
      const [facets, settings, list, st0, reach0] = await Promise.all([api('/facets'), api('/settings'), api('/campaigns'), api('/status'), api('/campaigns/reach')]);
      let showForm = list.length === 0;
      let timer;
      let known = [];   // the campaign rows last drawn
      let linked = !!st0.whatsapp_linked;
      let reach = reach0;

      function formHtml() {
        const w = settings.followup_wait_days;
        const chk = (name, vals) => vals.map((v) => html`<label class="opt"><input type="checkbox" name="${name}" value="${v}">${v}</label>`);
        const card = (ch, title, line) => html`<label><input type="radio" name="channel" value="${ch}"><b>${title}</b><span class="small">${reach[ch].leads} lead${reach[ch].leads === 1 ? '' : 's'} can be reached</span><span class="small muted">${line}</span></label>`;
        return html`<section class="panel" id="newpanel"><div class="panel-head"><h2>New campaign</h2><button class="btn small quiet right" id="hide-form">Close</button></div>
          <form id="cform" class="panel-body stack" style="gap:20px">
            <details><summary class="small muted" style="cursor:pointer">Not sure where to start? Let the AI fill this in</summary>
              <div class="stack" style="gap:8px;margin-top:8px"><textarea id="plan-goal" rows="2" placeholder="Reach the pharmacies in Meru about my stock dashboard and offer a quick look"></textarea>
                <div class="row"><button class="btn small primary" type="button" id="plan-go">Set up with AI</button></div><div id="plan-note"></div></div></details>

            <div class="stack" style="gap:8px"><h3>1. How do you want to reach them?</h3>
              <div class="reach">${card('whatsapp', 'WhatsApp', linked ? 'Connected: sends by itself' : 'Not connected: you press send')}
                ${card('email', 'Email', reach.email.ready ? 'Ready' : 'Set up Gmail in Settings first')}
                ${card('sms', 'Text message', reach.sms.ready ? 'Ready: sent by your phone' : 'Connect your phone in Settings first')}</div>
              <div class="small muted">Each lead gets one message at a time. Anyone who already has one is left out until it is a follow-up.</div></div>

            <div class="stack" style="gap:10px"><h3>2. Who gets it?</h3>
              <div class="grid2"><label class="field">Find by name<input type="text" id="aud-q" placeholder="Start typing a business name"></label></div>
              <div><div class="small muted" style="margin-bottom:4px">Sector</div><div class="chips chips-scroll">${facets.sectors.length ? chk('sectors', facets.sectors) : html`<span class="muted">No sectors yet</span>`}</div></div>
              <div><div class="small muted" style="margin-bottom:4px">Town</div><div class="chips chips-scroll">${facets.towns.length ? chk('towns', facets.towns) : html`<span class="muted">No towns yet</span>`}</div></div>
              <div id="pickhead" class="row"></div><div id="pick" class="pick"></div></div>

            <div class="stack" style="gap:10px"><h3>3. What should it say?</h3>
              <div class="chips"><label class="opt"><input type="radio" name="mode" value="ready" checked>Ready-made for each kind of business</label>
                <label class="opt"><input type="radio" name="mode" value="own">I'll write it</label>
                <label class="opt"><input type="radio" name="mode" value="ai">AI writes each one</label></div>
              <div class="small muted" id="mode-note"></div>
              <div id="msgbox" class="stack hidden" style="gap:8px">
                <label class="field hidden" id="subj-wrap">Subject<input type="text" name="subject" placeholder="A quick question for {name}"></label>
                <div class="field"><span id="msg-title">Message</span>
                  <textarea name="body" rows="5" placeholder="{name}: how much does {pain} cost you each month? I can show you. {website} - {first}"></textarea>
                  <div class="row tags" id="tools"><button type="button" class="btn small primary" id="ai-write" title="Uses anything you have typed as guidance">Write with AI</button>${TAGS.map((t) => html`<button type="button" class="btn small" data-tag="${t}">${t}</button>`)}</div>
                  <span class="smsmeter" id="meter"></span></div>
              </div>
              <div id="preview" class="stack" style="gap:8px"></div></div>

            <div class="stack" style="gap:8px"><h3>4. If they don't reply</h3>
              <select name="followups"><option value="">No follow-up</option><option value="${w}">One follow-up after ${w} day${w === 1 ? '' : 's'}</option><option value="${w},4" selected>Two: after ${w} day${w === 1 ? '' : 's'}, then 4 days later</option><option value="${w},4,7">Three: after ${w}, then 4, then 7 days later</option></select>
              <div class="small muted">Only people who haven't answered get one: a gentle nudge, then something useful, then a polite last note. Replies and opt-outs stop them.</div></div>

            <div class="row"><label class="field" style="flex:1;max-width:320px">Name (optional)<input type="text" name="name" placeholder="Meru pharmacies, October"></label></div>
            <div class="row"><button class="btn primary" type="button" id="go" disabled>Start campaign</button><button class="btn" type="button" id="draft">Save as draft</button></div>
          </form></section>`;
      }

      render(el, html`<div class="stack">
        <div class="row"><p class="muted">Messages go out one at a time within your daily limits and sending hours. Leads marked do-not-contact and bounced emails are always skipped, and nobody gets a second message unless it is a follow-up.</p>
          <button class="btn primary right ${showForm ? 'hidden' : ''}" id="new">New campaign</button></div>
        <div id="formwrap">${showForm ? formHtml() : ''}</div>
        <div id="waq"></div>
        <section class="panel"><div class="panel-head"><h2>Campaigns</h2></div><div id="clist"></div></section>
      </div>`);

      // ---------------------------------------------------------------- list
      async function drawList() {
        let rows; try { [rows, linked] = await Promise.all([api('/campaigns'), api('/status').then((x) => !!x.whatsapp_linked)]); } catch (_) { return; }
        known = rows;
        rows = rows.filter((c) => c.style !== 'single' || c.status !== 'done');   // one-lead WhatsApp sends show only while they are in progress
        render($('#clist'), rows.length ? html`<div class="tablewrap"><table class="t"><thead><tr><th>Campaign</th><th>Status</th><th style="width:170px">Progress</th><th>Failed</th><th>Replies</th><th></th></tr></thead><tbody>
          ${rows.map((c) => { const [lab, cls] = CSTATUS[c.status] || [c.status, '']; return html`<tr style="cursor:default">
            <td><div class="name">${c.name}</div><div class="muted small">${CHLAB[c.channel] || c.channel}${c.style === 'ready' ? ' · ready-made' : c.ai_personalize ? ' · AI-written' : ''}${c.followup_days || c.followup_of ? ` · follow-up${c.followup_days ? ` after ${c.followup_days} day${c.followup_days === 1 ? '' : 's'} of silence` : ''}` : ''} · ${c.launched_at ? 'Started ' + when(c.launched_at) : 'Not started'}</div>${c.last_error ? html`<div class="small" style="color:var(--bad)">${c.last_error}</div>` : ''}</td>
            <td><span class="chip ${cls}">${lab}</span></td>
            <td><div class="progress"><i style="width:${App.pct(c.sent, c.total)}%"></i></div><div class="small muted">${c.sent} of ${c.total} sent${c.queued ? ` · ${c.queued} waiting` : ''}</div></td>
            <td>${c.failed || '0'}${c.skipped ? html`<div class="small muted">${c.skipped} skipped</div>` : ''}</td>
            <td>${c.replies}${c.hot ? html` <span class="chip hot">${c.hot} hot</span>` : ''}</td>
            <td class="nowrap">${c.status === 'running' && c.channel === 'whatsapp' && !linked ? html`<button class="btn small primary" data-act="queue" data-id="${c.id}" title="WhatsApp isn't connected, so you press send for each lead">Send by hand</button> ` : ''}
              ${c.status === 'running' ? html`<button class="btn small" data-act="pause" data-id="${c.id}">Pause</button>` : ''}
              ${c.status === 'paused' ? html`<button class="btn small primary" data-act="resume" data-id="${c.id}">Resume</button>` : ''}
              ${c.status === 'draft' ? html`<button class="btn small primary" data-act="launch" data-id="${c.id}">Start sending</button>` : ''}
              ${c.sent > 0 && !c.followups_waiting && ['done', 'running', 'paused'].includes(c.status) ? html`<button class="btn small" data-act="followup" data-id="${c.id}" title="Message the people who haven't replied">Follow up</button>` : ''}
              ${c.failed && c.status !== 'cancelled' ? html`<button class="btn small" data-act="retry" data-id="${c.id}" title="Try failed messages again">Retry failed</button>` : ''}
              ${['running', 'paused', 'draft', 'scheduled'].includes(c.status) ? html`<button class="btn small danger" data-act="cancel" data-id="${c.id}">Cancel</button>` : ''}
              <button class="btn small quiet" data-act="view" data-id="${c.id}">Details</button></td></tr>`; })}
          </tbody></table></div>` : html`<div class="empty"><strong>No campaigns yet</strong>Press New campaign, choose how to reach people, tick the leads and start.</div>`);
      }
      await drawList();
      timer = setInterval(drawList, 5000);

      $('#clist').addEventListener('click', async (e) => {
        const b = e.target.closest('button[data-act]'); if (!b) return;
        const id = +b.dataset.id, act = b.dataset.act;
        try {
          if (act === 'view') return showDetails(id);
          if (act === 'queue') return openQueue(id);
          if (act === 'followup') return followUp(known.find((c) => c.id === id));
          if (act === 'cancel' && !(await App.confirm('Cancel this campaign? Messages that have not been sent yet will be dropped.', 'Cancel campaign', true))) return;
          if (act === 'launch' && !(await App.confirm('Start sending this campaign now?', 'Start sending'))) return;
          await api(`/campaigns/${id}/${act}`, { method: 'POST' });
          toast({ pause: 'Paused', resume: 'Resumed', cancel: 'Cancelled', launch: 'Sending started', retry: 'Failed messages queued again' }[act]);
          drawList(); App.pollStatus();
        } catch (err) { fail(err); }
      });

      async function followUp(c) {
        const dlg = await App.dialog({ title: 'Follow up', ok: 'Schedule follow-up', body: html`
          <p class="small muted">Goes only to people from “${c.name}” who got a message and haven't replied. Nobody ever gets two messages at once.</p>
          <label class="field">Send it by<select id="fu-ch">${['email', 'whatsapp', 'sms'].map((ch) => html`<option value="${ch}" ${ch === c.channel ? 'selected' : ''}>${CHLAB[ch]}</option>`)}</select></label>
          <label class="field">After how many days of silence<input id="fu-days" type="number" min="0" max="30" value="${settings.followup_wait_days}"></label>
          <p class="small muted">The message is ready-made for each kind of business: a gentle nudge first, then something useful, then a polite last note. Another channel works too, for leads reachable on it.</p>` });
        if (!dlg) return;
        try {
          await api(`/campaigns/${c.id}/followup`, { method: 'POST', body: { days: +$('#fu-days').value, channel: $('#fu-ch').value } });
          toast('Follow-up scheduled'); drawList(); App.pollStatus();
        } catch (err) { fail(err); }
      }

      async function showDetails(id) {
        const c = await api('/campaigns/' + id);
        const first = c.messages;
        App.dialog({ title: c.name, ok: 'Close', cancel: '', wide: true, body: html`
          <div class="muted small">${CHLAB[c.channel] || c.channel} · ${c.sent} sent · ${c.queued} waiting · ${c.failed} failed · ${c.replies} replies (${c.hot} hot, ${c.warm} warm)</div>
          <div class="sample">${c.style === 'ready' ? 'Ready-made messages, each written for the lead\'s kind of business.' : c.ai_personalize ? 'AI brief: ' + c.body : (c.channel === 'email' ? c.subject + '\n\n' : '') + c.body}</div>
          <div class="tablewrap" style="max-height:300px"><table class="t"><thead><tr><th>Business</th><th>To</th><th>Status</th><th>When</th></tr></thead><tbody>
          ${first.map((m) => html`<tr style="cursor:default"><td>${m.name}${m.body ? html`<details class="small"><summary>Message</summary><div class="sample">${m.subject ? m.subject + '\n\n' : ''}${m.body}</div></details>` : ''}</td><td class="small">${m.to_addr}</td>
            <td>${m.status === 'sent' ? html`<span class="chip ok">Sent</span>` : m.status === 'failed' ? html`<span class="chip bounce" title="${m.error}">Failed</span>` : m.status === 'skipped' ? html`<span class="chip cold" title="${m.error}">Skipped</span>` : html`<span class="chip">Waiting</span>`}
              ${m.error ? html`<div class="small muted">${m.error}</div>` : ''}</td><td class="small muted">${m.sent_at ? when(m.sent_at) : ''}</td></tr>`)}</tbody></table></div>` });
      }

      // ------------------------------------------------------- WhatsApp by hand (only while WhatsApp is not connected)
            async function openQueue(cid) {
        const box = $('#waq');
        const close = () => { render(box, html``); drawList(); };
        const next = async () => {
          let r; try { r = await api(`/campaigns/${cid}/whatsapp/next`); } catch (e) { render(box, html``); return fail(e); }
          if (!r.item) {
            render(box, html`<section class="panel"><div class="panel-body stack" style="gap:8px"><strong>All done</strong><div class="muted small">Everyone in this campaign has been dealt with. Replies you log on a lead are matched to the message you sent.</div><div><button class="btn small" id="waq-x">Close</button></div></div></section>`);
            $('#waq-x').onclick = close; return drawList();
          }
          show(r.item, false);
        };
        const show = (it, opened) => {
          const done = it.total - it.left;
          render(box, html`<section class="panel"><div class="panel-head"><h2>WhatsApp: ${done + 1} of ${it.total}</h2><button class="btn small quiet right" id="waq-x">Close</button></div>
            <div class="panel-body stack" style="gap:10px">
              <div><strong>${it.name}</strong><div class="muted small">${[it.town, it.sector].filter(Boolean).join(' · ')}${it.town || it.sector ? ' · ' : ''}${it.phone}</div></div>
              <textarea id="waq-body" rows="5" ${opened ? 'readonly' : ''}>${it.body}</textarea>
              <div class="row">${opened
                ? html`<button class="btn primary" id="waq-sent">I sent it</button><button class="btn quiet" id="waq-again">Open again</button>`
                : html`<button class="btn primary" id="waq-open">Open WhatsApp</button>`}
                <button class="btn small right" id="waq-no" title="Skips this lead here. If they have an email, an email campaign can still reach them.">Not on WhatsApp</button>
                <button class="btn small quiet" id="waq-skip">Skip</button></div>
            </div></section>`);
          const run = (btn, job) => async () => { btn.disabled = true; try { await job(); } catch (e) { fail(e); btn.disabled = false; } };
          const act = (action) => api(`/messages/${it.id}/whatsapp`, { method: 'POST', body: { action } });
          $('#waq-x').onclick = close;
          const open = async (btn) => { const r = await api(`/messages/${it.id}/whatsapp`, { method: 'POST', body: { action: 'link', body: $('#waq-body').value } });
            await api('/open-url', { method: 'POST', body: { url: r.url } }); it.body = $('#waq-body').value; show(it, true); };
          if ($('#waq-open')) $('#waq-open').onclick = (e) => run(e.currentTarget, () => open())();
          if ($('#waq-again')) $('#waq-again').onclick = (e) => run(e.currentTarget, async () => { const r = await api(`/messages/${it.id}/whatsapp`, { method: 'POST', body: { action: 'link' } }); await api('/open-url', { method: 'POST', body: { url: r.url } }); show(it, true); })();
          if ($('#waq-sent')) $('#waq-sent').onclick = (e) => run(e.currentTarget, async () => { await act('sent'); toast('Logged as sent'); await next(); })();
          $('#waq-no').onclick = (e) => run(e.currentTarget, async () => { await act('no_whatsapp'); toast('Marked as not on WhatsApp'); await next(); })();
          $('#waq-skip').onclick = (e) => run(e.currentTarget, async () => { await act('skip'); await next(); })();
        };
        await next();
      }

      // ---------------------------------------------------------------- form
      $('#new').onclick = async () => { reach = await api('/campaigns/reach'); $('#formwrap').innerHTML = formHtml().s; $('#new').classList.add('hidden'); bindForm(); };
      if (showForm) bindForm();

      function bindForm() {
        const f = $('#cform'); if (!f) return;
        let picked = new Set(), shown = [], total = 0, lastPreview = null;
        const channel = () => f.elements.channel.value;
        const mode = () => f.elements.mode.value;
        // start on a way that is ready and has leads
        const first = ['whatsapp', 'email', 'sms'].find((c) => reach[c].ready && reach[c].leads && (c !== 'whatsapp' || linked)) || ['email', 'sms', 'whatsapp'].find((c) => reach[c].ready && reach[c].leads) || 'email';
        f.querySelector(`input[name=channel][value=${first}]`).checked = true;

        const audienceFilters = () => { const fd = new FormData(f); return { sectors: fd.getAll('sectors'), towns: fd.getAll('towns'), q: $('#aud-q').value.trim() }; };
        const getBody = () => {
          const fd = new FormData(f);
          return { name: fd.get('name'), channel: channel(), subject: fd.get('subject') || '', body: mode() === 'ready' ? '' : fd.get('body'),
            filters: { lead_ids: [...picked] }, ai: mode() === 'ai', style: mode() === 'ready' ? 'ready' : '',
            followups: (fd.get('followups') || '').split(',').filter(Boolean).map(Number) };
        };

        // ---- the leads, each one tickable
        const drawPick = () => {
          render($('#pickhead'), html`<div class="small"><b>${picked.size}</b> of ${total} selected</div><div class="row right" style="gap:6px"><button type="button" class="btn small" id="pick-all">Select all shown</button><button type="button" class="btn small quiet" id="pick-none">Clear</button></div>`);
          render($('#pick'), shown.length ? html`${shown.map((l) => html`<label><input type="checkbox" data-id="${l.id}" ${picked.has(l.id) ? 'checked' : ''}><span style="flex:1"><b>${l.name}</b> <span class="muted small">${[l.town, l.sector].filter(Boolean).join(' · ')}</span></span><span class="small muted">${l.contact}</span></label>`)}`
            : html`<div class="empty" style="padding:18px"><strong>Nobody to show</strong>Everyone who can be reached this way has already been messaged, or no one matches the search.</div>`);
          $('#pick-all').onclick = () => { shown.forEach((l) => picked.add(l.id)); drawPick(); preview(); };
          $('#pick-none').onclick = () => { picked.clear(); drawPick(); preview(); };
          if (total > shown.length) $('#pickhead').insertAdjacentHTML('beforeend', `<div class="small muted" style="width:100%">Showing the first ${shown.length} of ${total}. Narrow by sector, town or name to see the rest.</div>`);
        };
        $('#pick').addEventListener('change', (e) => { const id = +e.target.dataset.id; if (!id) return; e.target.checked ? picked.add(id) : picked.delete(id); render($('#pickhead').firstElementChild, html`<b>${picked.size}</b> of ${total} selected`); preview(); });
        const loadAudience = async (selectAll) => {
          try {
            const r = await api('/campaigns/audience', { method: 'POST', body: { channel: channel(), filters: audienceFilters(), limit: 1000 } });
            shown = r.leads; total = r.total;
            if (selectAll) picked = new Set(shown.map((l) => l.id)); else picked = new Set([...picked].filter((id) => shown.some((l) => l.id === id)));
            drawPick(); preview();
          } catch (err) { fail(err); }
        };

        // ---- the message
        const plainHint = f.elements.body.placeholder;
        const aiHint = 'Offer my stock dashboard to pharmacies and ask if they would like a quick look. Keep it friendly.';
        const syncForm = () => {
          const ch = channel(), m = mode();
          $('#msgbox').classList.toggle('hidden', m === 'ready');
          $('#subj-wrap').classList.toggle('hidden', ch !== 'email' || m !== 'own'); $('#tools').classList.toggle('hidden', m !== 'own');
          $('#msg-title').textContent = m === 'ai' ? 'What should the AI say? Give the goal, the offer and what you want them to do.' : 'Message';
          f.elements.body.placeholder = m === 'ai' ? aiHint : plainHint;
          $('#mode-note').textContent = m === 'ready' ? 'Each business gets the message written for its kind of business (pharmacy, agrovet, hardware, salon…), with its own name, a real pain point, and your website, WhatsApp and email. Texts are always one SMS of 160 characters or less.'
            : m === 'own' ? 'Use {name}, {town}, {pain}, {website}, {whatsapp} and {email} and each lead gets their own version.'
            : 'The AI writes a fresh message for each person, in the same plain, curious style, and keeps texts to one SMS.';
          if (ch !== 'sms' || m === 'ai' || m === 'ready') return render($('#meter'), html``);
          const len = f.elements.body.value.length + (settings.append_optout ? (settings.optout_suffix_sms || '').length : 0);
          $('#meter').textContent = `${len} of 160 characters before names are filled in`; $('#meter').style.color = len > 160 ? 'var(--bad)' : '';
        };
        const preview = debounce(async () => {
          if (!document.body.contains(f)) return;   // user already left this page
          syncForm(); const body = getBody(); const box = $('#preview'); $('#go').disabled = true; lastPreview = null;
          if (!picked.size) return render(box, html`<div class="notice">Tick at least one lead above.</div>`);
          if (mode() !== 'ready' && !body.body.trim()) return render(box, html`<div class="muted">Write your message to see an example.</div>`);
          try {
            const p = await api('/campaigns/preview', { method: 'POST', body });
            if (!document.body.contains(f)) return;
            lastPreview = p;
            const cap = settings[`${body.channel}_daily_cap`] || 1;
            const days = p.will_send ? Math.ceil(p.will_send / Math.max(1, cap)) : 0;
            render(box, html`
              ${!p.channel_ready ? html`<div class="notice bad">${body.channel === 'sms' ? 'Your phone isn\'t connected yet.' : 'Email isn\'t connected yet.'} <a href="#/settings">Open settings</a> before sending.</div>` : ''}
              ${body.channel === 'whatsapp' && !p.whatsapp_linked ? html`<div class="notice">WhatsApp isn't connected, so you'll press send for each lead. <a href="#/settings">Connect it in Settings</a> and it sends by itself.</div>` : ''}
              ${body.ai && !p.ai_ready ? html`<div class="notice bad">The AI isn't connected yet. <a href="#/settings">Open settings</a> and add a free key, or choose a ready-made message.</div>` : ''}
              <div><strong>${p.will_send}</strong> lead${p.will_send === 1 ? '' : 's'} will get ${body.ai ? 'a message written for them' : 'a message'}${days > 1 ? html` <span class="muted">· at your daily limit of ${cap} that takes about ${days} days</span>` : ''}.</div>
              ${p.too_long ? html`<div class="notice bad">${p.too_long} text${p.too_long === 1 ? ' is' : 's are'} too long for one SMS and would be skipped. Shorten the message.</div>` : ''}
              ${body.ai ? html`<div class="row"><span class="small muted">Each message is written by the AI just before it goes out.</span><button type="button" class="btn small right" id="ai-samples">Show example messages</button></div><div id="samples" class="stack" style="gap:8px"></div>`
                : p.sample ? html`<div class="small muted">Example for ${p.sample.lead}, sent to ${p.sample.to}${p.sample.length ? ` · ${p.sample.length} of 160 characters` : ''}</div>
                <div class="sample">${p.sample.subject ? p.sample.subject + '\n\n' : ''}${p.sample.body}</div>` : ''}`);
            if ($('#ai-samples')) $('#ai-samples').onclick = showSamples;
            $('#go').disabled = !p.will_send || !p.channel_ready || (body.ai && !p.ai_ready) || p.too_long === p.will_send;
          } catch (err) { render(box, html`<div class="notice">${err.message}</div>`); }
        }, 400);
        const busy = async (btn, working, job) => {
          const label = btn.textContent; btn.disabled = true; btn.textContent = working;
          try { await job(); } catch (err) { fail(err); } finally { btn.disabled = false; btn.textContent = label; }
        };
        const showSamples = (e) => busy(e.currentTarget, 'Writing…', async () => {
          const rows = await api('/campaigns/ai-samples', { method: 'POST', body: getBody(), timeout: 100000 });
          render($('#samples'), html`${rows.map((x) => html`<div class="small muted">Example for ${x.lead}, sent to ${x.to}</div><div class="sample">${x.subject ? x.subject + '\n\n' : ''}${x.body}</div>`)}
            <div class="small muted">Examples only: each real message is written fresh when it is sent.</div>`);
        });
        $('#plan-go').onclick = (e) => busy(e.currentTarget, 'Setting up…', async () => {
          const goal = $('#plan-goal').value.trim(); if (!goal) return toast('Say who you want to reach first');
          const r = await api('/campaigns/ai-plan', { method: 'POST', body: { goal }, timeout: 100000 });
          render($('#plan-note'), r.reply ? html`<div class="notice">${r.reply}</div>` : html``);
          if (!r.brief) return;
          f.elements.name.value = r.name; f.querySelector(`input[name=channel][value=${r.channel}]`).checked = true;
          f.querySelector('input[name=mode][value=ai]').checked = true; f.elements.body.value = r.brief;
          $$('input[name="sectors"]', f).forEach((c) => (c.checked = r.sectors.includes(c.value)));
          $$('input[name="towns"]', f).forEach((c) => (c.checked = r.towns.includes(c.value)));
          await loadAudience(true);
        });

        // a change of way to reach, sector or town starts the list again; typing a name only narrows it
        f.addEventListener('change', (e) => { const n = e.target.name; if (n === 'channel' || n === 'sectors' || n === 'towns') loadAudience(true); else preview(); });
        $('#aud-q').addEventListener('input', debounce(() => loadAudience(false), 300));
        f.addEventListener('input', (e) => { if (e.target.id !== 'aud-q') preview(); });
        $$('[data-tag]', f).forEach((b) => (b.onclick = () => { const ta = f.elements.body; ta.setRangeText(b.dataset.tag, ta.selectionStart, ta.selectionEnd, 'end'); ta.focus(); preview(); }));
        $('#ai-write').onclick = (e) => App.aiFill(e.currentTarget, '/ai/template', channel(), f);
        $('#hide-form').onclick = () => { render($('#formwrap'), html``); $('#new').classList.remove('hidden'); };

        const submit = async (launch) => {
          try {
            const n = lastPreview ? lastPreview.will_send : picked.size, ch = channel();
            const ask = ch === 'whatsapp' ? (linked ? `Start sending on WhatsApp to ${n} lead${n === 1 ? '' : 's'}? Messages go out one at a time, with pauses, from your connected WhatsApp.` : `Start a WhatsApp campaign for ${n} lead${n === 1 ? '' : 's'}? WhatsApp isn't connected, so you'll press send for each lead.`)
              : ch === 'sms' ? `Start texting ${n} lead${n === 1 ? '' : 's'}? Your phone sends them one at a time, with a pause between each.` : `Start sending to ${n} lead${n === 1 ? '' : 's'}? Emails go out gradually within your limits.`;
            if (launch && !(await App.confirm(ask, 'Start campaign'))) return;
            await api('/campaigns', { method: 'POST', body: { ...getBody(), launch } });
            toast(launch ? 'Campaign started' : 'Saved as draft');
            render($('#formwrap'), html``); $('#new').classList.remove('hidden'); drawList(); App.pollStatus();
          } catch (err) { fail(err); }
        };
        $('#go').onclick = () => submit(true);
        $('#draft').onclick = () => submit(false);
        syncForm(); loadAudience(true);
      }
      return () => clearInterval(timer);
    },
  };
})();
