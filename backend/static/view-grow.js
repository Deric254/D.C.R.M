(function () {
  const { html, render, api, $, $$, toast, fail } = App;
  const TABS = [['content', 'Content'], ['partners', 'Partners']];
  const STATUS = { to_contact: 'To contact', contacted: 'Contacted', replied: 'Replied', agreed: 'Agreed', declined: 'Declined' };
  let tab = 'content';

  const copy = async (text) => {
    try { await navigator.clipboard.writeText(text); toast('Copied'); }
    catch (e) { toast('Select the text and copy it by hand'); }
  };

  App.views.grow = {
    title: 'Grow',
    async render(el) {
      render(el, html`<div class="stack">
        <p class="muted">Ways to reach many businesses without sending anything from here. You review and post everything yourself.</p>
        <div class="tabs" id="tabs">${TABS.map(([k, l]) => html`<button class="tab ${k === tab ? 'active' : ''}" data-tab="${k}">${l}</button>`)}</div>
        <div id="body" class="stack"></div></div>`);

      // ---------------------------------------------------------------- content
      const PLATS = [['linkedin', 'LinkedIn'], ['facebook', 'Facebook'], ['whatsapp_status', 'WhatsApp Status'], ['tiktok', 'TikTok / Reels script']];
      const postCard = (p) => html`<div class="panel"><div class="panel-head"><h2>${p.label || p.platform}</h2>
          <span class="right row"><button class="btn small" data-copy="${p.id || ''}" data-text-of="p-${p.id || p.platform}">Copy</button></span></div>
        <div class="panel-body"><textarea id="p-${p.id || p.platform}" rows="${p.platform === 'whatsapp_status' ? 3 : 8}" style="width:100%">${p.body}</textarea></div></div>`;

      async function drawContent() {
        const box = $('#body');
        render(box, html`
          <section class="panel"><div class="panel-head"><h2>Turn one finding into posts</h2></div><div class="panel-body stack" style="gap:12px">
            <p class="muted small">Write one thing you found in a business's records, without naming the business, for example "one pharmacy had KES 400,000 tied up in 12 slow-moving products". Never name a client without their permission. The drafts use only what you write, so check each one before posting.</p>
            <label class="field">The finding<textarea id="finding" rows="3" maxlength="800" placeholder="What did you find?"></textarea></label>
            <label class="field">Who is it for? (optional)<input id="audience" placeholder="pharmacy owners in Nairobi"></label>
            <div class="row" style="gap:14px;flex-wrap:wrap">${PLATS.map(([k, l]) => html`<label class="row small" style="gap:6px"><input type="checkbox" data-plat="${k}" ${k === 'linkedin' || k === 'facebook' || k === 'whatsapp_status' ? 'checked' : ''}> ${l}</label>`)}</div>
            <div class="row"><button class="btn primary" id="draft">Write the drafts</button><span class="muted small" id="draft-note"></span></div>
          </div></section>
          <div id="fresh" class="stack"></div>
          <section class="panel"><div class="panel-head"><h2>Earlier drafts</h2></div><div class="panel-body stack" id="older" style="gap:10px"></div></section>`);
        await drawOlder();
      }
      async function drawOlder() {
        const r = await api('/grow/posts').catch(() => ({ posts: [], platforms: {} }));
        render($('#older'), r.posts.length ? r.posts.slice(0, 12).map((p) => html`<div class="stack" style="gap:6px;border-bottom:1px solid var(--line);padding-bottom:10px">
          <div class="row"><b class="small">${r.platforms[p.platform] || p.platform}</b><span class="muted small">${App.when ? App.when(p.created_at) : p.created_at}</span>
            <span class="right row"><button class="btn small" data-copytext="${p.id}">Copy</button><button class="btn small" data-delpost="${p.id}">Delete</button></span></div>
          <div class="small" style="white-space:pre-wrap" id="old-${p.id}">${p.body}</div></div>`) : html`<p class="muted small">Nothing yet. Your drafts are kept here.</p>`);
      }

      // --------------------------------------------------------------- partners
      let kinds = [], statuses = [], pitchFor = null;
      async function drawPartners() {
        const r = await api('/grow/partners'); kinds = r.kinds; statuses = r.statuses;
        render($('#body'), html`
          <section class="panel"><div class="panel-head"><h2>Add a partner</h2></div><div class="panel-body stack" style="gap:12px">
            <p class="muted small">Associations, chambers, software vendors, accountants and county offices. One yes can reach all their members. This list is separate from your leads, so no campaign ever messages a partner by accident.</p>
            <div class="grid2"><label class="field">Name<input id="pn" placeholder="Pharmaceutical Society of Kenya"></label>
              <label class="field">Kind<select id="pk">${kinds.map((k) => html`<option>${k}</option>`)}</select></label>
              <label class="field">Contact person<input id="pc"></label><label class="field">Email<input id="pe" type="email"></label>
              <label class="field">Phone<input id="pp"></label></div>
            <div><button class="btn primary" id="padd">Add partner</button></div></div></section>
          <section class="panel"><div class="panel-head"><h2>Your partners</h2><span class="chip right">${r.partners.length}</span></div>
            <div class="panel-body stack" style="gap:12px" id="plist">${r.partners.length ? r.partners.map((p) => html`<div class="stack" style="gap:8px;border-bottom:1px solid var(--line);padding-bottom:12px">
              <div class="row" style="gap:10px;flex-wrap:wrap"><b>${p.name}</b><span class="chip">${p.kind}</span>
                <span class="muted small">${[p.contact, p.email, p.phone].filter(Boolean).join(' · ')}</span>
                <span class="right row"><select data-status="${p.id}" style="width:auto">${statuses.map((s) => html`<option value="${s}" ${s === p.status ? 'selected' : ''}>${STATUS[s]}</option>`)}</select>
                  <button class="btn small" data-pitch="${p.id}">Write pitch</button><button class="btn small" data-delp="${p.id}">Delete</button></span></div>
              <div class="grid2"><input data-step="${p.id}" placeholder="Next step" value="${p.next_step}"><input data-note="${p.id}" placeholder="Notes" value="${p.note}"></div>
              ${pitchFor && pitchFor.id === p.id ? html`<div class="notice" style="margin:0"><b>${pitchFor.subject}</b><div class="small" style="white-space:pre-wrap;margin:8px 0">${pitchFor.body}</div>
                <div class="row"><a class="btn small primary" href="mailto:${pitchFor.to || ''}?subject=${encodeURIComponent(pitchFor.subject)}&body=${encodeURIComponent(pitchFor.body)}">Open in my email</a>
                  <button class="btn small" id="pcopy">Copy</button><span class="muted small">You send it yourself, from your own email.</span></div></div>` : ''}</div>`)
              : html`<p class="muted small">No partners yet.</p>`}</div></section>`);
      }

      const draw = () => (tab === 'content' ? drawContent() : drawPartners()).catch(fail);
      draw();

      el.addEventListener('click', async (e) => {
        const t = e.target.closest('[data-tab]');
        if (t) { tab = t.dataset.tab; pitchFor = null; $$('#tabs .tab').forEach((b) => b.classList.toggle('active', b === t)); return draw(); }
        try {
          if (e.target.closest('#draft')) {
            const finding = $('#finding').value.trim(); const platforms = $$('[data-plat]').filter((c) => c.checked).map((c) => c.dataset.plat);
            const b = e.target.closest('#draft'); b.disabled = true; $('#draft-note').textContent = 'Writing…';
            try {
              const r = await api('/grow/posts', { method: 'POST', body: { finding, platforms, audience: $('#audience').value }, timeout: 120000 });
              $('#draft-note').textContent = r.ai ? '' : 'Written from plain templates: set up your AI in Settings for fuller drafts.';
              render($('#fresh'), r.posts.map(postCard)); drawOlder();
            } finally { b.disabled = false; if (!$('#draft-note').textContent.startsWith('Written')) $('#draft-note').textContent = ''; }
            return;
          }
          const c = e.target.closest('[data-text-of]'); if (c) return copy($('#' + c.dataset.textOf).value);
          const ct = e.target.closest('[data-copytext]'); if (ct) return copy($('#old-' + ct.dataset.copytext).textContent);
          const dp = e.target.closest('[data-delpost]'); if (dp) { await api('/grow/posts/' + dp.dataset.delpost, { method: 'DELETE' }); return drawOlder(); }
          if (e.target.closest('#padd')) {
            await api('/grow/partners', { method: 'POST', body: { name: $('#pn').value, kind: $('#pk').value, contact: $('#pc').value, email: $('#pe').value, phone: $('#pp').value } });
            toast('Partner added'); return drawPartners();
          }
          const pt = e.target.closest('[data-pitch]');
          if (pt) { pt.disabled = true; pitchFor = { id: +pt.dataset.pitch, ...(await api(`/grow/partners/${pt.dataset.pitch}/pitch`, { method: 'POST', timeout: 120000 })) }; return drawPartners(); }
          if (e.target.closest('#pcopy')) return copy(pitchFor.subject + '\n\n' + pitchFor.body);
          const dl = e.target.closest('[data-delp]');
          if (dl && confirm('Delete this partner?')) { await api('/grow/partners/' + dl.dataset.delp, { method: 'DELETE' }); return drawPartners(); }
        } catch (err) { fail(err); }
      });
      el.addEventListener('change', async (e) => {
        try {
          const s = e.target.closest('[data-status]');
          if (s) { await api('/grow/partners/' + s.dataset.status, { method: 'PUT', body: { status: s.value } }); toast('Saved'); return drawPartners(); }
          const st = e.target.closest('[data-step]');
          if (st) { await api('/grow/partners/' + st.dataset.step, { method: 'PUT', body: { next_step: st.value } }); return toast('Saved'); }
          const n = e.target.closest('[data-note]');
          if (n) { await api('/grow/partners/' + n.dataset.note, { method: 'PUT', body: { note: n.value } }); return toast('Saved'); }
        } catch (err) { fail(err); }
      });
    },
  };
})();
