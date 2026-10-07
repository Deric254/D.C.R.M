(function () {
  const { html, render, api, $, $$, when, toast, fail } = App;
  const CATS = ['Pharmacy', 'Chemist', 'Agrovet', 'Wholesale', 'Supermarket', 'Minimart', 'Hardware', 'Clinic', 'Cereals shop', 'Hospital', 'Dental clinic', 'Laboratory', 'Veterinary clinic', 'Cosmetics shop', 'Boutique', 'Salon', 'Barbershop', 'Restaurant', 'Hotel', 'Butchery', 'Bakery', 'Grocery', 'General shop', 'Electronics shop', 'Phone repair', 'Mobile money agent', 'Stationery shop', 'Bookshop', 'School', 'Nursery school', 'Gym', 'Garage', 'Car wash', 'Spare parts', 'Furniture shop', 'Building materials', 'Plumbing supplies', 'Printing shop', 'Cyber cafe', 'Law firm', 'Accounting firm', 'Real estate agent', 'Insurance agent', 'Sacco', 'Microfinance', 'Travel agency', 'Courier', 'Sawmill', 'Tailor', 'Laundry', 'Photo studio', 'Event hall', 'Petrol station', 'Farm supplies', 'Dairy', 'Fish shop', 'Gas supplier'];
  const TOWNS = ['Nairobi', 'Mombasa', 'Kisumu', 'Nakuru', 'Eldoret', 'Thika', 'Machakos', 'Kitui', 'Meru', 'Nanyuki', 'Nyeri', 'Embu', 'Chuka', 'Nkubu', 'Maua', 'Karatina', 'Kerugoya', 'Kiambu', 'Ruiru', 'Juja', 'Limuru', 'Naivasha', 'Nyahururu', 'Kericho', 'Bomet', 'Kisii', 'Migori', 'Homa Bay', 'Kakamega', 'Bungoma', 'Webuye', 'Kitale', 'Busia', 'Siaya', 'Garissa', 'Isiolo', 'Marsabit', 'Kajiado', 'Ngong', 'Kitengela', 'Athi River', 'Narok', 'Voi', 'Malindi', 'Kilifi', 'Lamu', 'Kwale', 'Ukunda', 'Mtwapa', 'Nyamira', 'Vihiga', 'Mumias', 'Lodwar', 'Wajir', 'Mandera'];
  const ORIGINAL_SIX = [['Pharmacy', 'Meru'], ['Agrovet', 'Chuka'], ['Wholesale', 'Nkubu'], ['Chemist', 'Embu'], ['Supermarket', 'Thika'], ['Minimart', 'Karatina']];
  const KEY = 'dericbi.find.v1';

  const DEFAULTS = { cats: ['Pharmacy', 'Agrovet'], towns: ['Meru'], searches: [], max: 60, show: false, enrich: false, customCats: [], customTowns: [] };
  function load() {
    try { return Object.assign({}, DEFAULTS, JSON.parse(localStorage.getItem(KEY) || '{}')); }
    catch (_) { return { ...DEFAULTS }; }
  }
  const save = (S) => { try { localStorage.setItem(KEY, JSON.stringify(S)); } catch (_) { /* private mode */ } };

  App.views.find = {
    title: 'Find leads',
    async render(el) {
      const S = load();
      let viewJob = null, lastLog = 0, timer = null;
      const alive = () => document.body.contains(el) && !!$('#log');

      const KINDS = {
        cat: { title: 'Business types', base: CATS, custom: 'customCats', sel: 'cats' },
        town: { title: 'Towns', base: TOWNS, custom: 'customTowns', sel: 'towns' },
      };
      const Q = { cat: '', town: '' };
      const listOf = (k) => [...new Set([...KINDS[k].base, ...S[KINDS[k].custom]])].sort((a, b) => a.localeCompare(b));
      const shown = (k) => listOf(k).filter((x) => x.toLowerCase().includes(Q[k]));
      const hasSearch = (c, tw) => S.searches.some(([a, b]) => a === c && b === tw);

      function drawChips(k) {
        render($('#chips-' + k), shown(k).map((x) => html`<label class="opt"><input type="checkbox" data-pick="${k}" value="${x}" ${S[KINDS[k].sel].includes(x) ? 'checked' : ''}>${x}</label>`));
      }
      function drawSummary() {
        Object.entries(KINDS).forEach(([k, K]) => { $('#count-' + k).textContent = `${S[K.sel].length} selected`; });
        const n = S.cats.length * S.towns.length;
        $('#mix').disabled = !n;
        $('#mix').textContent = `Add ${n} search${n === 1 ? '' : 'es'}`;
      }
      function drawBuilder() {
        render($('#builder'), html`<div class="stack" style="gap:16px">
          ${Object.entries(KINDS).map(([k, K]) => html`<div>
            <div class="row" style="margin-bottom:8px"><h3>${K.title}</h3><span class="muted small" id="count-${k}"></span>
              <span class="right row" style="gap:4px"><button type="button" class="btn small quiet" data-all="${k}">Select shown</button><button type="button" class="btn small quiet" data-none="${k}">Clear</button></span></div>
            <input type="search" data-filter="${k}" value="${Q[k]}" placeholder="Search the list" aria-label="Search ${K.title}" style="margin-bottom:8px">
            <div class="chips chips-scroll" id="chips-${k}"></div>
            <form data-add="${k}" class="row" style="gap:4px;margin-top:8px"><input type="text" placeholder="Add your own (separate several with commas)" aria-label="Add to ${K.title}" style="flex:1;min-width:200px"><button class="btn small">Add</button></form>
          </div>`)}
          <div class="row"><button class="btn" id="mix"></button><button class="btn quiet" id="orig">Use my original six</button></div>
        </div>`);
        drawChips('cat'); drawChips('town'); drawSummary();
      }
      function drawQueue() {
        render($('#queue'), html`
          <div class="row" style="margin-bottom:8px"><h3>Searches to run (${S.searches.length})</h3>
            ${S.searches.length ? html`<button class="btn small quiet right" id="clear">Clear all</button>` : ''}</div>
          ${S.searches.length ? html`<div class="chips chips-scroll">${S.searches.map(([c, t], i) => html`<span class="opt" style="cursor:default">${c} in ${t}<button class="btn small quiet" data-rm="${i}" aria-label="Remove ${c} in ${t}" style="padding:0 4px">×</button></span>`)}</div>`
            : html`<div class="muted">Pick business types and towns above, then add them here.</div>`}`);
        const busy = !!(App.state.status && App.state.status.job);
        $('#start').disabled = !S.searches.length || busy;
        $('#start').textContent = busy ? 'A job is running' : `Start finding leads${S.searches.length ? ` (${S.searches.length})` : ''}`;
      }

      render(el, html`<div class="grid2" style="align-items:start">
        <div class="stack">
          <section class="panel"><div class="panel-head"><h2>Ask the AI</h2><button type="button" class="btn small quiet right hidden" id="plan-new">Start over</button></div>
            <div class="panel-body stack" style="gap:12px">
              <div id="plan-out" class="stack" style="gap:12px"></div>
              <form id="plan-form" class="stack" style="gap:8px">
                <textarea id="plan-text" rows="2" aria-label="Tell the AI what you want" placeholder="Tell the AI who you want to reach. For example: pharmacies and clinics in Kisii and Migori that could use my billing software."></textarea>
                <div class="row"><button class="btn primary" id="plan-ask">Ask AI</button><span class="muted small">It suggests the searches. Nothing runs until you press a start button.</span></div>
              </form>
            </div></section>
          <section class="panel"><div class="panel-head"><h2>What to look for</h2></div><div class="panel-body stack" style="gap:16px">
            <div id="builder"></div><hr class="sep"><div id="queue"></div>
            <hr class="sep">
            <div class="grid2">
              <label class="field">New leads to keep per search<input type="number" id="max" min="1" max="300" value="${S.max}"><span class="hint">Stops a search after this many new businesses.</span></label>
              <div class="stack" style="gap:8px;align-content:start;padding-top:20px">
                <label class="check"><input type="checkbox" id="show" ${S.show ? 'checked' : ''}>Show the browser window <span class="muted small">(only if Google asks for a CAPTCHA)</span></label>
                <label class="check"><input type="checkbox" id="enrich" ${S.enrich ? 'checked' : ''}>Look for emails on their websites after</label></div>
            </div>
            <div class="row"><button class="btn primary" id="start">Start</button>
              <button class="btn" id="emails">Look for emails now</button></div>
            <p class="muted small">Businesses already in your CRM (even archived ones) are skipped automatically, so you can run the same searches again safely. Searches run quietly in the background. If Google ever shows a CAPTCHA the run stops: tick “Show the browser window”, run it again and solve it there.</p>
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

      // ---- AI planner: you talk, it proposes searches, you add them (or add and start in one click)
      const chat = [];   // what you and the AI have said so far
      let plan = [];     // suggested [category, town] pairs that are not added yet
      function drawPlan() {
        $('#plan-new').classList.toggle('hidden', !chat.length && !plan.length);
        render($('#plan-out'), html`
          ${chat.length ? html`<div class="plan-chat" id="plan-chat">${chat.map((m) => html`<div class="plan-msg ${m.role}">${m.text}</div>`)}</div>` : ''}
          ${plan.length ? html`<div>
            <div class="muted small" style="margin-bottom:6px">${plan.length} suggested search${plan.length === 1 ? '' : 'es'}. Remove any you don't want:</div>
            <div class="chips chips-scroll">${plan.map(([c, t], i) => html`<span class="opt" style="cursor:default">${c} in ${t}<button type="button" class="btn small quiet" data-plan-rm="${i}" aria-label="Remove ${c} in ${t}" style="padding:0 4px">×</button></span>`)}</div>
            <div class="row" style="margin-top:10px"><button type="button" class="btn" id="plan-add">Add to my searches</button><button type="button" class="btn primary" id="plan-go">Add and start finding leads</button></div>
          </div>` : ''}`);
        const box = $('#plan-chat'); if (box) box.scrollTop = box.scrollHeight;
      }
      async function askPlan() {
        const ta = $('#plan-text'), btn = $('#plan-ask'), goal = ta.value.trim();
        if (!goal) return;
        btn.disabled = true; btn.textContent = 'Thinking…';
        try {
          const r = await api('/ai/plan-searches', { method: 'POST', body: { goal, history: chat }, timeout: 60000 });
          chat.push({ role: 'user', text: goal }, { role: 'ai', text: r.reply || (r.searches.length ? 'Here is what I would search.' : 'Tell me a little more about who you want to reach.') });
          if (r.searches.length) plan = r.searches.map((x) => [x.category, x.town]);   // the AI saw the whole conversation, so its newest list replaces the old one
          ta.value = ''; drawPlan();
        } catch (err) { fail(err); }
        finally { if (alive()) { btn.disabled = false; btn.textContent = 'Ask AI'; } }
      }
      function addPlan() {
        let n = 0;
        plan.forEach(([c, tw]) => { if (!hasSearch(c, tw)) { S.searches.push([c, tw]); n++; } });
        plan = []; save(S); drawQueue(); drawPlan();
        toast(n ? `Added ${n} search${n === 1 ? '' : 'es'}` : 'Those searches were already in your list');
      }

      drawBuilder(); drawQueue(); drawPlan();
      const pick = (k, v, on) => { const sel = KINDS[k].sel; S[sel] = on ? [...new Set([...S[sel], v])] : S[sel].filter((x) => x !== v); };
      el.addEventListener('change', (e) => {
        const t = e.target;
        if (t.dataset.pick) { pick(t.dataset.pick, t.value, t.checked); save(S); drawSummary(); }
        else if (t.id === 'show') { S.show = t.checked; save(S); }
        else if (t.id === 'enrich') { S.enrich = t.checked; save(S); }
        else if (t.id === 'max') { S.max = Math.max(1, Math.min(300, +t.value || 60)); save(S); }
      });
      el.addEventListener('keydown', (e) => {
        if (e.target.id === 'plan-text' && e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); askPlan(); }
      });
      el.addEventListener('input', (e) => {
        const k = e.target.dataset.filter;
        if (k) { Q[k] = e.target.value.trim().toLowerCase(); drawChips(k); }
      });
      el.addEventListener('submit', (e) => {
        if (e.target.id === 'plan-form') { e.preventDefault(); askPlan(); return; }
        const k = e.target.dataset.add; if (!k) return;
        e.preventDefault();
        const input = e.target.querySelector('input');
        const have = new Set(listOf(k).map((x) => x.toLowerCase()));
        input.value.split(/[,\n;]/).map((x) => x.trim()).filter(Boolean).forEach((v) => {
          if (!have.has(v.toLowerCase())) { S[KINDS[k].custom].push(v); have.add(v.toLowerCase()); }
          pick(k, listOf(k).find((x) => x.toLowerCase() === v.toLowerCase()), true);
        });
        input.value = ''; save(S); drawChips(k); drawSummary();
      });
      async function startRun() {
        try {
          const r = await api('/jobs/scrape', { method: 'POST', body: { searches: S.searches.map(([category, town]) => ({ category, town })), max_per_search: S.max, headless: !S.show, enrich: S.enrich } });
          viewJob = r.id; lastLog = 0; $('#log').textContent = ''; toast('Started'); App.pollStatus(); tick();
        } catch (err) { fail(err); }
      }
      el.addEventListener('click', async (e) => {
        const t = e.target.closest('button'); if (!t) return;
        if (t.dataset.all) { shown(t.dataset.all).forEach((v) => pick(t.dataset.all, v, true)); save(S); drawChips(t.dataset.all); drawSummary(); }
        else if (t.dataset.none) { S[KINDS[t.dataset.none].sel] = []; save(S); drawChips(t.dataset.none); drawSummary(); }
        else if (t.id === 'mix') {
          S.cats.forEach((c) => S.towns.forEach((tw) => { if (!hasSearch(c, tw)) S.searches.push([c, tw]); }));
          save(S); drawQueue();
        } else if (t.id === 'orig') {
          ORIGINAL_SIX.forEach(([c, tw]) => { if (!hasSearch(c, tw)) S.searches.push([c, tw]); });
          save(S); drawQueue();
        } else if (t.id === 'clear') { S.searches = []; save(S); drawQueue(); }
        else if (t.dataset.rm !== undefined) { S.searches.splice(+t.dataset.rm, 1); save(S); drawQueue(); }
        else if (t.id === 'start') { await startRun(); }
        else if (t.id === 'plan-add') { addPlan(); }
        else if (t.id === 'plan-go') { addPlan(); await startRun(); }
        else if (t.dataset.planRm !== undefined) { plan.splice(+t.dataset.planRm, 1); drawPlan(); }
        else if (t.id === 'plan-new') { chat.length = 0; plan = []; drawPlan(); }
        else if (t.id === 'emails') {
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
        if (App.state.status && !!App.state.status.job !== ['queued', 'running'].includes(job.status)) { App.pollStatus().then(() => alive() && drawQueue()); }
      }
      await tick();
      timer = setInterval(tick, 2000);
      return () => clearInterval(timer);
    },
  };
})();
