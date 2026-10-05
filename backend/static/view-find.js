(function () {
  const { html, render, api, $, $$, when, toast, fail } = App;
  const CATS = ['Pharmacy', 'Chemist', 'Agrovet', 'Wholesale', 'Supermarket', 'Minimart', 'Hardware', 'Clinic', 'Cereals shop'];
  const TOWNS = ['Meru', 'Chuka', 'Nkubu', 'Embu', 'Thika', 'Karatina', 'Nyeri', 'Nanyuki', 'Maua', 'Kerugoya'];
  const ORIGINAL_SIX = [['Pharmacy', 'Meru'], ['Agrovet', 'Chuka'], ['Wholesale', 'Nkubu'], ['Chemist', 'Embu'], ['Supermarket', 'Thika'], ['Minimart', 'Karatina']];
  const KEY = 'dericbi.find.v1';

  function load() {
    try { return Object.assign({ cats: ['Pharmacy', 'Agrovet'], towns: ['Meru'], searches: [], max: 60, visible: true, enrich: false, customCats: [], customTowns: [] }, JSON.parse(localStorage.getItem(KEY) || '{}')); }
    catch (_) { return { cats: ['Pharmacy', 'Agrovet'], towns: ['Meru'], searches: [], max: 60, visible: true, enrich: false, customCats: [], customTowns: [] }; }
  }
  const save = (S) => { try { localStorage.setItem(KEY, JSON.stringify(S)); } catch (_) { /* private mode */ } };

  App.views.find = {
    title: 'Find leads',
    async render(el) {
      const S = load();
      let viewJob = null, lastLog = 0, timer = null;
      const alive = () => document.body.contains(el) && !!$('#log');

      const chips = (name, list, selected) => list.map((x) => html`<label class="opt"><input type="checkbox" data-${name}="${x}" ${selected.includes(x) ? 'checked' : ''}>${x}</label>`);
      function draw() {
        render($('#builder'), html`
          <div class="stack" style="gap:14px">
            <div><h3 style="margin-bottom:8px">Business types</h3>
              <div class="chips">${chips('cat', [...CATS, ...S.customCats], S.cats)}
                <form id="add-cat" class="row" style="gap:4px"><input type="text" placeholder="Add another type" style="width:150px" aria-label="Add business type"><button class="btn small">Add</button></form></div></div>
            <div><h3 style="margin-bottom:8px">Towns</h3>
              <div class="chips">${chips('town', [...TOWNS, ...S.customTowns], S.towns)}
                <form id="add-town" class="row" style="gap:4px"><input type="text" placeholder="Add another town" style="width:150px" aria-label="Add town"><button class="btn small">Add</button></form></div></div>
            <div class="row"><button class="btn" id="mix" ${S.cats.length && S.towns.length ? '' : 'disabled'}>Add ${S.cats.length * S.towns.length} search${S.cats.length * S.towns.length === 1 ? '' : 'es'}</button>
              <button class="btn quiet" id="orig">Use my original six</button></div>
          </div>`);
        render($('#queue'), html`
          <div class="row" style="margin-bottom:8px"><h3>Searches to run (${S.searches.length})</h3>
            ${S.searches.length ? html`<button class="btn small quiet right" id="clear">Clear all</button>` : ''}</div>
          ${S.searches.length ? html`<div class="chips">${S.searches.map(([c, t], i) => html`<span class="opt" style="cursor:default">${c} in ${t}<button class="btn small quiet" data-rm="${i}" aria-label="Remove ${c} in ${t}" style="padding:0 4px">×</button></span>`)}</div>`
            : html`<div class="muted">Pick business types and towns above, then add them here.</div>`}`);
        const busy = !!(App.state.status && App.state.status.job);
        $('#start').disabled = !S.searches.length || busy;
        $('#start').textContent = busy ? 'A job is running' : `Start finding leads${S.searches.length ? ` (${S.searches.length})` : ''}`;
      }

      render(el, html`<div class="grid2" style="align-items:start">
        <div class="stack">
          <section class="panel"><div class="panel-head"><h2>What to look for</h2></div><div class="panel-body stack" style="gap:16px">
            <div id="builder"></div><hr class="sep"><div id="queue"></div>
            <hr class="sep">
            <div class="grid2">
              <label class="field">New leads to keep per search<input type="number" id="max" min="1" max="300" value="${S.max}"><span class="hint">Stops a search after this many new businesses.</span></label>
              <div class="stack" style="gap:8px;align-content:start;padding-top:20px">
                <label class="check"><input type="checkbox" id="visible" ${S.visible ? 'checked' : ''}>Show the browser window</label>
                <label class="check"><input type="checkbox" id="enrich" ${S.enrich ? 'checked' : ''}>Look for emails on their websites after</label></div>
            </div>
            <div class="row"><button class="btn primary" id="start">Start</button>
              <button class="btn" id="emails">Look for emails now</button></div>
            <p class="muted small">Businesses already in your CRM (even archived ones) are skipped automatically, so you can run the same searches again safely. Keep the window visible: if Google asks for a CAPTCHA you can solve it there and the run continues.</p>
          </div></section>
        </div>
        <div class="stack">
          <section class="panel"><div class="panel-head"><h2 id="job-title">Progress</h2><span id="job-chip"></span><button class="btn small danger right hidden" id="stop">Stop</button></div>
            <div class="panel-body stack" style="gap:12px">
              <div class="muted" id="job-progress">Nothing has run yet.</div>
              <div class="stats4" id="job-stats"></div>
              <div class="logbox" id="log" aria-live="off" tabindex="0"></div>
            </div></section>
          <section class="panel"><div class="panel-head"><h2>Recent runs</h2></div><div id="history"></div></section>
        </div>
      </div>`);

      draw();
      el.addEventListener('change', (e) => {
        const t = e.target;
        if (t.dataset.cat !== undefined) { t.checked ? S.cats.push(t.dataset.cat) : (S.cats = S.cats.filter((x) => x !== t.dataset.cat)); save(S); draw(); }
        else if (t.dataset.town !== undefined) { t.checked ? S.towns.push(t.dataset.town) : (S.towns = S.towns.filter((x) => x !== t.dataset.town)); save(S); draw(); }
        else if (t.id === 'visible') { S.visible = t.checked; save(S); }
        else if (t.id === 'enrich') { S.enrich = t.checked; save(S); }
        else if (t.id === 'max') { S.max = Math.max(1, Math.min(300, +t.value || 60)); save(S); }
      });
      el.addEventListener('submit', (e) => {
        const id = e.target.id; if (id !== 'add-cat' && id !== 'add-town') return;
        e.preventDefault();
        const v = e.target.querySelector('input').value.trim(); if (!v) return;
        const [all, custom, sel] = id === 'add-cat' ? [CATS, 'customCats', 'cats'] : [TOWNS, 'customTowns', 'towns'];
        if (!all.includes(v) && !S[custom].includes(v)) S[custom].push(v);
        if (!S[sel].includes(v)) S[sel].push(v);
        save(S); draw();
      });
      el.addEventListener('click', async (e) => {
        const t = e.target.closest('button'); if (!t) return;
        if (t.id === 'mix') {
          S.cats.forEach((c) => S.towns.forEach((tw) => { if (!S.searches.some(([a, b]) => a === c && b === tw)) S.searches.push([c, tw]); }));
          save(S); draw();
        } else if (t.id === 'orig') {
          ORIGINAL_SIX.forEach(([c, tw]) => { if (!S.searches.some(([a, b]) => a === c && b === tw)) S.searches.push([c, tw]); });
          save(S); draw();
        } else if (t.id === 'clear') { S.searches = []; save(S); draw(); }
        else if (t.dataset.rm !== undefined) { S.searches.splice(+t.dataset.rm, 1); save(S); draw(); }
        else if (t.id === 'start') {
          try {
            const r = await api('/jobs/scrape', { method: 'POST', body: { searches: S.searches.map(([category, town]) => ({ category, town })), max_per_search: S.max, headless: !S.visible, enrich: S.enrich } });
            viewJob = r.id; lastLog = 0; $('#log').textContent = ''; toast('Started'); App.pollStatus(); tick();
          } catch (err) { fail(err); }
        } else if (t.id === 'emails') {
          try { const r = await api('/jobs/enrich', { method: 'POST' }); viewJob = r.id; lastLog = 0; $('#log').textContent = ''; App.pollStatus(); tick(); } catch (err) { fail(err); }
        } else if (t.id === 'stop') {
          try { await api(`/jobs/${viewJob}/stop`, { method: 'POST' }); toast('Stopping after the current business…'); } catch (err) { fail(err); }
        } else if (t.dataset.job) { viewJob = +t.dataset.job; lastLog = 0; $('#log').textContent = ''; tick(); }
      });

      const STAT = { queued: ['Starting', ''], running: ['Running', 'ok'], done: ['Finished', 'ok'], stopped: ['Stopped', 'warm'], failed: ['Failed', 'bounce'] };
      async function tick() {
        let jobs;
        try { jobs = await api('/jobs'); } catch (_) { return; }
        if (!alive()) return;
        const active = jobs.find((j) => ['queued', 'running'].includes(j.status));
        if (viewJob == null) viewJob = (active || jobs[0] || {}).id ?? null;
        const job = jobs.find((j) => j.id === viewJob);
        render($('#history'), jobs.length ? html`<div class="list">${jobs.map((j) => html`
          <div><div class="grow"><div>${j.kind === 'enrich' ? 'Email search' : `${(j.params.searches || []).length} searches`}</div><div class="muted small">${when(j.created_at)} · ${j.progress || ''}</div></div>
          <button class="btn small quiet" data-job="${j.id}">View log</button></div>`)}</div>` : html`<div class="empty">Runs will be listed here.</div>`);
        if (!job) return;
        const [label, cls] = STAT[job.status] || [job.status, ''];
        $('#job-chip').innerHTML = `<span class="chip ${cls}">${label}</span>`;
        $('#job-progress').textContent = job.progress || '';
        $('#stop').classList.toggle('hidden', !['queued', 'running'].includes(job.status));
        const stats = job.kind === 'enrich'
          ? [['Sites checked', job.found], ['Emails found', job.added], ['', ''], ['Errors', job.errors]]
          : [['Listings seen', job.found], ['New leads', job.added], ['Already saved', job.duplicates], ['Couldn\'t read', job.errors]];
        render($('#job-stats'), html`${stats.map(([k, v]) => html`<div><b>${v}</b><span class="muted small">${k}</span></div>`)}`);
        const r = await api(`/jobs/${job.id}/logs?after=${lastLog}`).catch(() => null);
        if (!alive()) return;
        if (r && r.logs.length) {
          const box = $('#log'); const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
          r.logs.forEach((l) => { box.append(document.createTextNode(l.ts.slice(11) + '  ' + l.msg + '\n')); lastLog = l.id; });
          if (stick) box.scrollTop = box.scrollHeight;
        }
        if (App.state.status && !!App.state.status.job !== ['queued', 'running'].includes(job.status)) { App.pollStatus().then(() => alive() && draw()); }
      }
      await tick();
      timer = setInterval(tick, 2000);
      return () => clearInterval(timer);
    },
  };
})();
