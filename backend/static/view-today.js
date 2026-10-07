(function () {
  const { html, render, api, when, $ } = App;

  // Who needs you now, and the quickest way to answer each. The message is written for you when it opens.
  App.views.today = {
    title: 'Today',
    async render(el) {
      const d = await api('/today');
      const total = d.replies.length + d.due.length + d.quiet.length;
      const row = (l, why) => {
        const wa = l.is_mobile, mail = l.email && !l.email_bounced;
        return html`<div class="item" style="gap:10px"><div class="grow"><div><a href="#" data-open="${l.id}" style="color:inherit;font-weight:650">${l.name}</a> ${l.grade ? html`<span class="chip ${l.grade}">${App.GRADE_LABEL[l.grade] || l.grade}</span>` : ''}</div>
            <div class="muted small">${l.town || ''}${l.sector ? ' · ' + l.sector : ''} · ${why(l)}</div></div>
          <span class="nowrap">${wa ? html`<button class="btn small primary" data-open="${l.id}" data-ch="whatsapp">WhatsApp</button>` : ''}
            ${mail ? html` <button class="btn small" data-open="${l.id}" data-ch="email">Email</button>` : ''}
            ${!wa && !mail ? html`<button class="btn small" data-open="${l.id}">Open</button>` : ''}</span></div>`;
      };
      const section = (title, hint, rows, why) => html`<section class="panel"><div class="panel-head"><h2>${title}</h2><span class="muted small right">${rows.length}</span></div>
        ${rows.length ? html`<div class="list">${rows.map((l) => row(l, why))}</div>` : html`<div class="empty"><strong>Nothing here</strong>${hint}</div>`}</section>`;

      render(el, html`<div class="stack">
        <p class="muted">${total ? `${total} lead${total === 1 ? '' : 's'} need you. Press a button and the message is already written for that person: answer, follow-up or first hello. Check it, then send.` : 'You are all caught up. Find more leads or start a campaign.'}</p>
        ${section('They replied', 'Replies you have not dealt with appear here, hottest first.', d.replies, (l) => `replied ${when(l.last_reply_at)}`)}
        ${section('Follow-ups due', 'Set a follow-up date on a lead and it shows here on the day.', d.due, (l) => `follow-up date ${l.next_followup}`)}
        ${section(`No reply after ${d.wait_days} days`, 'Leads you messaged that stay quiet show up here.', d.quiet,
          (l) => `${l.unanswered} message${l.unanswered === 1 ? '' : 's'} sent, last ${when(l.last_contacted_at)}, no answer`)}
      </div>`);

      $('.stack', el).addEventListener('click', (e) => {   // on this page's own box, so it never stacks up across visits
        const b = e.target.closest('[data-open]'); if (!b) return;
        e.preventDefault(); App.openLead(+b.dataset.open, b.dataset.ch);
      });
    },
  };
})();
