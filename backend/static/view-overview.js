(function () {
  const { html, render, api, when } = App;
  const FLOW = [['new', 'New'], ['contacted', 'Contacted'], ['replied', 'Replied'], ['interested', 'Interested'], ['meeting', 'Meeting'], ['won', 'Won']];

  App.views.overview = {
    title: 'Overview',
    async render(el) {
      const [d, st] = await Promise.all([api('/dashboard'), api('/status')]);
      const s = d.by_status;
      const sent = d.sent_today;
      const firstRun = d.total === 0;

      const guide = (firstRun || (!st.email_ready && !st.sms_ready)) && html`
        <section class="panel"><div class="panel-body stack" style="gap:10px">
          <h2>Getting started</h2>
          <ol style="margin:0;padding-left:20px;display:grid;gap:6px">
            ${d.total === 0 && html`<li>Bring in leads: <a href="#/find">find them on Google Maps</a>, or <a href="#/leads">import your existing CSV</a>. Anything already saved is never added twice.</li>`}
            ${!st.sms_ready && html`<li>Connect SMS (Africa's Talking) in <a href="#/settings">Settings</a> to text leads that have mobile numbers.</li>`}
            ${!st.email_ready && html`<li>Connect your email in <a href="#/settings">Settings</a> to email leads and track their replies.</li>`}
            <li>Write your message in <a href="#/outreach">Outreach</a>, preview who it goes to, then start sending.</li>
          </ol></div></section>`;

      render(el, html`<div class="stack">
        ${guide}
        <section class="panel"><div class="panel-body stack" style="gap:12px">
          <div class="row"><h2>Pipeline</h2><span class="muted small">${d.total} active leads${s.lost ? ` · ${s.lost} lost` : ''}</span>
            <a class="btn small right" href="#/leads">Open leads</a></div>
          <div class="pipeline">${FLOW.map(([k, label], i) => html`
            <a class="seg s${i}" href="#/leads?status=${k}" title="Show ${label.toLowerCase()} leads"><b>${s[k] || 0}</b><span>${label}</span></a>`)}</div>
        </div></section>

        <div class="kpis">
          <div class="panel kpi"><b>${sent.sms + sent.email}</b><span>sent today (${sent.sms} SMS, ${sent.email} email)</span></div>
          <div class="panel kpi"><b>${d.replies_week}</b><span>replies this week</span></div>
          <div class="panel kpi"><b>${d.reply_rate}%</b><span>of contacted leads replied</span></div>
          <div class="panel kpi"><b>${d.by_grade.hot || 0}</b><span>hot leads</span></div>
          <div class="panel kpi"><b>${d.with_mobile}</b><span>have a mobile number</span></div>
          <div class="panel kpi"><b>${d.with_email}</b><span>have an email</span></div>
        </div>

        <div class="grid2">
          <section class="panel"><div class="panel-head"><h2>Hot leads</h2><a class="small right" href="#/leads?grade=hot">See all</a></div>
            ${d.hot.length ? html`<div class="list">${d.hot.map((l) => html`
              <a class="item" data-lead="${l.id}"><div class="grow"><div>${l.name}</div><div class="muted small">${l.town || ''}${l.sector ? ' · ' + l.sector : ''}</div></div>
              <span class="muted small nowrap">${when(l.last_reply_at)}</span></a>`)}</div>`
              : html`<div class="empty"><strong>No hot leads yet</strong>Replies that ask for prices, details or a call show up here.</div>`}
          </section>
          <section class="panel"><div class="panel-head"><h2>Follow-ups due</h2></div>
            ${d.due.length ? html`<div class="list">${d.due.map((l) => html`
              <a class="item" data-lead="${l.id}"><div class="grow"><div>${l.name}</div><div class="muted small">${l.town || ''}</div></div>
              <span class="chip warm nowrap">${l.next_followup}</span></a>`)}</div>`
              : html`<div class="empty"><strong>Nothing due</strong>Set a follow-up date on a lead and it will appear here.</div>`}
          </section>
        </div>

        <div class="grid2">
          <section class="panel"><div class="panel-head"><h2>Running campaigns</h2><a class="small right" href="#/outreach">Manage</a></div>
            ${d.campaigns.length ? html`<div class="list">${d.campaigns.map((c) => html`
              <div><div class="grow"><div>${c.name}</div><div class="muted small">${c.channel === 'sms' ? 'SMS' : 'Email'} · ${c.sent} of ${c.total} sent · ${c.replies} replies${c.last_error ? ' · ' + c.last_error : ''}</div></div>
              <span class="chip ${c.status === 'paused' ? 'warm' : 'ok'}">${c.status === 'paused' ? 'Paused' : 'Running'}</span></div>`)}</div>`
              : html`<div class="empty"><strong>Nothing is sending</strong><a href="#/outreach">Start a campaign</a></div>`}
          </section>
          <section class="panel"><div class="panel-head"><h2>Recent activity</h2></div>
            ${d.activity.length ? html`<div class="list">${d.activity.map((a) => html`
              <a class="item" data-lead="${a.lead_id}"><div class="grow"><div>${a.name}</div><div class="muted small">${a.detail}</div></div>
              <span class="muted small nowrap">${when(a.ts)}</span></a>`)}</div>`
              : html`<div class="empty">Activity will appear as you work.</div>`}
          </section>
        </div>
      </div>`);

      el.addEventListener('click', (e) => {
        const a = e.target.closest('[data-lead]');
        if (a) App.openLead(+a.dataset.lead);
      });
      const t = setInterval(() => { if (location.hash.startsWith('#/overview') || location.hash === '') App.refresh(); }, 30000);
      return () => clearInterval(t);
    },
  };
})();
