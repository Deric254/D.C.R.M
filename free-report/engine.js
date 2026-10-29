/* Free Business Health Check: the reading and analysis engine.
 *
 * Takes almost any sales file a business keeps (CSV or Excel, tidy or messy), cleans it, and accounts for EVERY row and EVERY number:
 * each row ends up in exactly one bucket (sale, return, total row left out, zero value, unreadable, blank, repeated heading), and the
 * amounts in the buckets add up to the sum of every number read from the file. Nothing is dropped silently.
 *
 * No DOM here: it runs in the browser and in Node (for the tests).
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.ReportEngine = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  /* ------------------------------------------------------------------ reading bytes */
  function decodeBytes(buf) {
    const u8 = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
    let enc = 'utf-8', start = 0;
    if (u8.length >= 3 && u8[0] === 0xef && u8[1] === 0xbb && u8[2] === 0xbf) { start = 3; }
    else if (u8.length >= 2 && u8[0] === 0xff && u8[1] === 0xfe) { enc = 'utf-16le'; start = 2; }
    else if (u8.length >= 2 && u8[0] === 0xfe && u8[1] === 0xff) { enc = 'utf-16be'; start = 2; }
    else if (u8.length >= 4 && u8[1] === 0 && u8[3] === 0 && u8[0] !== 0) { enc = 'utf-16le'; }   // UTF-16 without a BOM
    const body = start ? u8.subarray(start) : u8;
    try { return { text: new TextDecoder(enc, { fatal: true }).decode(body), encoding: enc }; }
    catch (e) { return { text: new TextDecoder('windows-1252').decode(body), encoding: 'windows-1252' }; }   // old Excel "CSV" files
  }

  function parseCSV(text) {
    text = text.replace(/^﻿/, '');
    let sepLine = text.match(/^sep=(.)\r?\n/i);
    let forced = null;
    if (sepLine) { forced = sepLine[1]; text = text.slice(sepLine[0].length); }
    const sample = text.split(/\r?\n/).slice(0, 25).filter((l) => l.trim());
    const score = (d) => {   // the delimiter that gives the same number of fields on most lines wins
      const counts = sample.map((l) => { let n = 1, q = false; for (const c of l) { if (c === '"') q = !q; else if (c === d && !q) n++; } return n; });
      const most = counts.sort((a, b) => a - b)[Math.floor(counts.length / 2)] || 1;
      return most > 1 ? most * (counts.filter((c) => c === most).length / counts.length) : 0;
    };
    const delim = forced || [',', ';', '\t', '|'].map((d) => [d, score(d)]).sort((a, b) => b[1] - a[1])[0][0];
    const rows = []; let row = [], cur = '', q = false;
    for (let i = 0; i < text.length; i++) {
      const c = text[i];
      if (q) { if (c === '"') { if (text[i + 1] === '"') { cur += '"'; i++; } else q = false; } else cur += c; }
      else if (c === '"' && cur === '') q = true;
      else if (c === delim) { row.push(cur); cur = ''; }
      else if (c === '\n' || c === '\r') { if (c === '\r' && text[i + 1] === '\n') i++; row.push(cur); rows.push(row); row = []; cur = ''; }
      else cur += c;
    }
    if (cur !== '' || row.length) { row.push(cur); rows.push(row); }
    return rows;
  }

  /** Bytes of a file -> { sheets: [{ name, rows }], notes }. `XLSX` is the SheetJS library (needed only for Excel files). */
  function readFile(buf, filename, XLSX) {
    const name = String(filename || '').toLowerCase();
    const u8 = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
    const isZip = u8.length > 3 && u8[0] === 0x50 && u8[1] === 0x4b;            // .xlsx is a zip
    const isOle = u8.length > 4 && u8[0] === 0xd0 && u8[1] === 0xcf && u8[2] === 0x11;   // old .xls
    if (/\.xlsx?$|\.xlsm$|\.ods$/.test(name) || isZip || isOle) {
      if (!XLSX) throw new Error('Excel files could not be opened here. Save your sheet as CSV (File > Save as > CSV) and upload that instead.');
      let wb;
      try { wb = XLSX.read(u8, { type: 'array', cellDates: true }); }
      catch (e) { throw new Error('That Excel file could not be opened. It may be damaged or password protected. Save it again as CSV and try that.'); }
      const sheets = wb.SheetNames.map((n) => ({ name: n, rows: XLSX.utils.sheet_to_json(wb.Sheets[n], { header: 1, raw: true, defval: '', blankrows: true }) }))
        .filter((s) => s.rows.some((r) => r.some((c) => String(c).trim() !== '')));
      if (!sheets.length) throw new Error('That Excel file has no data in it.');
      return { sheets, notes: [] };
    }
    const d = decodeBytes(u8), notes = [];
    if (d.encoding !== 'utf-8') notes.push(`Read the file as ${d.encoding}.`);
    const rows = parseCSV(d.text);
    if (!rows.some((r) => r.some((c) => String(c).trim() !== ''))) throw new Error('That file is empty.');
    return { sheets: [{ name: 'File', rows }], notes };
  }

  /* ------------------------------------------------------------------ cell helpers */
  const MONTHS = ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october', 'november', 'december'];
  const isBlank = (v) => v == null || (typeof v === 'string' && v.replace(/[\s ]/g, '') === '') ;
  const txt = (v) => (v instanceof Date ? '' : String(v == null ? '' : v).replace(/[   ]/g, ' ').trim());

  /** A number from whatever people type: "KES 1,200.50", "1.200,50", "(500)", "500-", "1 200", "Ksh. 3,000/=". NaN when it is not a number. */
  function num(v) {
    if (typeof v === 'number') return isFinite(v) ? v : NaN;
    if (v instanceof Date || v == null) return NaN;
    let s = txt(v);
    if (!s || /^(n\/?a|nil|none|null|-+|\.|—|–|pending|tbc|tba)$/i.test(s)) return NaN;
    let neg = false;
    if (/^\(.*\)$/.test(s)) { neg = true; s = s.slice(1, -1); }
    if (/-\s*$/.test(s)) { neg = true; s = s.replace(/-\s*$/, ''); }
    if (/^[-−–]/.test(s)) { neg = true; s = s.replace(/^[-−–]\s*/, ''); }
    s = s.replace(/\/=|\/-/g, '').replace(/^(kes|ksh|kshs|k\.?sh\.?s?|usd|ugx|tzs|rwf|us\$|\$|€|£)\.?\s*/i, '').replace(/\s*(kes|ksh|kshs|usd|\/=)$/i, '');
    if (!/^[0-9][0-9\s.,']*$/.test(s) && !/^[.,][0-9]+$/.test(s)) return NaN;
    s = s.replace(/[\s']/g, '');
    const lastDot = s.lastIndexOf('.'), lastComma = s.lastIndexOf(',');
    if (lastDot >= 0 && lastComma >= 0) {   // both: the later one is the decimal mark
      s = lastComma > lastDot ? s.replace(/\./g, '').replace(',', '.') : s.replace(/,/g, '');
    } else if (lastComma >= 0) {
      s = /^\d{1,3}(,\d{3})+$/.test(s) ? s.replace(/,/g, '') : s.replace(',', '.');   // 1,200 is a thousand; 1,5 is one and a half
    } else if (lastDot >= 0) {
      if (/^\d{1,3}(\.\d{3}){2,}$/.test(s)) s = s.replace(/\./g, '');   // 1.200.000
    }
    const n = parseFloat(s);
    return isNaN(n) ? NaN : (neg ? -n : n);
  }

  const looksNumeric = (v) => typeof v === 'number' || (!isBlank(v) && !isNaN(num(v)));

  function monthIndex(s) {
    const t = txt(s).toLowerCase().replace(/[^a-z]/g, '');
    if (t.length < 3) return -1;
    return MONTHS.findIndex((m) => m === t || (t.length >= 3 && m.startsWith(t) && t.length <= m.length));
  }

  /** Decide whether a date column is day-first or month-first by looking at the whole column, not one cell. */
  function dateOrder(values) {
    let dmy = 0, mdy = 0;
    for (const v of values) {
      const m = typeof v === 'string' && txt(v).match(/^(\d{1,2})[-\/.](\d{1,2})[-\/.](\d{2,4})/);
      if (!m) continue;
      if (+m[1] > 12) dmy++; else if (+m[2] > 12) mdy++;
    }
    return mdy > dmy ? 'mdy' : 'dmy';   // Kenya writes the day first, so that is the default when it cannot be told
  }

  function toDate(v, order) {
    if (v instanceof Date) return isNaN(v) ? null : v;
    if (v == null || v === '') return null;
    if (typeof v === 'number') return v > 20000 && v < 80000 ? new Date(Math.round((v - 25569) * 86400 * 1000) + new Date().getTimezoneOffset() * 60000) : null;
    const s = txt(v); if (!s) return null;
    let m = s.match(/^(\d{4})[-\/.](\d{1,2})[-\/.](\d{1,2})/);
    if (m) return mk(+m[1], +m[2], +m[3]);
    m = s.match(/^(\d{1,2})[-\/.](\d{1,2})[-\/.](\d{2,4})/);
    if (m) { let y = +m[3]; if (y < 100) y += 2000; let d = +m[1], mo = +m[2]; if (order === 'mdy') [d, mo] = [mo, d]; if (mo > 12 && d <= 12) [d, mo] = [mo, d]; return mk(y, mo, d); }
    m = s.match(/^(\d{1,2})[-\/. ]([A-Za-z]{3,9})[-\/. ,]*(\d{2,4})/);   // 5-Jan-26, 5 January 2026
    if (m && monthIndex(m[2]) >= 0) { let y = +m[3]; if (y < 100) y += 2000; return mk(y, monthIndex(m[2]) + 1, +m[1]); }
    m = s.match(/^([A-Za-z]{3,9})[-\/. ]+(\d{1,2})(?:st|nd|rd|th)?[, ]+(\d{4})/);   // January 5, 2026
    if (m && monthIndex(m[1]) >= 0) return mk(+m[3], monthIndex(m[1]) + 1, +m[2]);
    m = s.match(/^([A-Za-z]{3,9})[-\/. ]+(\d{2,4})$/);   // Jan-26, January 2026 (a month, taken as its 1st)
    if (m && monthIndex(m[1]) >= 0) { let y = +m[2]; if (y < 100) y += 2000; return mk(y, monthIndex(m[1]) + 1, 1); }
    const t = Date.parse(s); const d = isNaN(t) ? null : new Date(t);
    return d && d.getFullYear() > 1990 && d.getFullYear() < 2100 ? d : null;
  }
  function mk(y, m, d) { if (m < 1 || m > 12 || d < 1 || d > 31) return null; const dt = new Date(y, m - 1, d); return dt.getMonth() === m - 1 ? dt : null; }

  /* ------------------------------------------------------------------ finding the table inside a sheet */
  /** The heading row is found by what the rows look like, so title lines, blank lines and notes above it are skipped. */
  function findTable(rows) {
    const width = rows.reduce((a, r) => Math.max(a, r.length), 0);
    const nonEmpty = (r) => r.filter((c) => !isBlank(c)).length;
    const limit = Math.min(rows.length, 60);
    let maxNE = 0; for (let i = 0; i < limit; i++) maxNE = Math.max(maxNE, nonEmpty(rows[i]));
    let h = -1;
    for (let i = 0; i < limit; i++) {
      const r = rows[i], ne = nonEmpty(r);
      if (ne < 2 || ne < Math.max(2, Math.floor(maxNE * 0.6))) continue;
      const cells = r.filter((c) => !isBlank(c));
      const textual = cells.filter((c) => !looksNumeric(c) && !(c instanceof Date)).length;
      if (textual / cells.length < 0.7) continue;
      let below = 0;   // data must follow
      for (let j = i + 1; j < Math.min(rows.length, i + 8); j++) if (rows[j].some((c) => looksNumeric(c))) below++;
      if (below >= 1) { h = i; break; }
    }
    if (h < 0) {   // fall back: the first row with at least two filled cells
      h = rows.findIndex((r) => nonEmpty(r) >= 2);
      if (h < 0) throw new Error('We could not find a table of rows and columns in that file.');
    }
    let headers = rows[h].concat(Array(Math.max(0, width - rows[h].length)).fill(''));
    let first = h + 1, mergedHeader = false;
    // a second heading line (for example "Sales" above "Qty | Amount"): join the two
    const nx = rows[first];
    if (nx && nonEmpty(nx) >= 2 && !nx.some((c) => looksNumeric(c) || c instanceof Date) && nonEmpty(nx) >= nonEmpty(rows[h]) * 0.5) {
      headers = headers.map((t, i) => [txt(t), txt(nx[i])].filter(Boolean).join(' '));
      first++; mergedHeader = true;
    }
    headers = headers.map((t, i) => txt(t) || 'Column ' + (i + 1));
    const seen = new Map();   // duplicate headings stay distinct
    headers = headers.map((t) => { const k = t.toLowerCase(); const n = (seen.get(k) || 0) + 1; seen.set(k, n); return n > 1 ? `${t} (${n})` : t; });
    return { headers, rows: rows.slice(first), firstDataRow: first + 1, titleRows: h, mergedHeader };
  }

  /* ------------------------------------------------------------------ column guessing */
  const SYN = {
    product: /item|product|description|particular|goods|article|sku|name|bidhaa|jina|maelezo|service|stock item|commodity|drug|medicine/i,
    amount: /^(total|amount|sales?|revenue|value|net|gross|line ?total|sub ?total)\b|amount|revenue|jumla|kiasi|takings|income|sales? value|total (sales|amount|price|value)/i,
    qty: /qty|quantity|units?|pcs|pieces|sold|idadi|vipande|no\.? of/i,
    price: /price|rate|unit ?cost|selling|bei|each/i,
    date: /date|tarehe|siku|day|time|when|period|month/i,
    receipt: /receipt|invoice|inv\b|transaction|trans\b|order|bill|ref|docket|risiti|voucher|sale ?id|till/i,
    stock: /stock|balance|on ?hand|closing|remaining|inventory|baki|available|opening/i
  };

  function colStats(rows, i) {
    let n = 0, numeric = 0, dates = 0, distinct = new Set(), sum = 0, textLen = 0;
    for (const r of rows) {
      const v = r[i]; if (isBlank(v)) continue; n++;
      if (v instanceof Date) { dates++; continue; }
      if (looksNumeric(v)) { numeric++; sum += Math.abs(num(v)); } else { textLen += txt(v).length; }
      if (distinct.size < 5000) distinct.add(txt(v).toLowerCase());
    }
    return { n, numeric, dates, distinct: distinct.size, sum, avgLen: n - numeric ? textLen / (n - numeric) : 0 };
  }

  /** Which column is which. Headings first (English and Swahili), then what the cells look like. */
  function guess(table) {
    const { headers, rows } = table;
    const sample = rows.slice(0, 3000);
    const stats = headers.map((_, i) => colStats(sample, i));
    const map = {}, used = new Set(), how = {};
    const take = (k, i, h) => { map[k] = i; used.add(i); how[k] = h; };
    const order = ['date', 'receipt', 'stock', 'qty', 'price', 'amount', 'product'];
    const NOT_PRODUCT = /customer|client|cashier|staff|seller|supplier|branch|shop|outlet|location|payment|mode|method|category|type|region|town|user|agent|salesperson|vendor|phone|email/i;
    const STRONG_PRODUCT = /item|product|description|particular|goods|article|sku|bidhaa|commodity|drug|medicine|service|maelezo/i;
    // wide layout first: months (or dates) across the top and one product per row. Decided from the headings alone.
    const periodCols0 = headers.map((h, i) => ({ i, h, p: periodOf(h) })).filter((c) => c.p && stats[c.i].n && stats[c.i].numeric / stats[c.i].n > 0.5);
    if (periodCols0.length >= 3) {
      let pi = -1, bs = 0;
      headers.forEach((h, i) => { if (periodCols0.some((c) => c.i === i)) return; const s = stats[i]; if (!s.n || s.numeric / s.n > 0.3 || NOT_PRODUCT.test(h)) return; const sc = s.distinct + (STRONG_PRODUCT.test(h) ? 1000 : 0); if (sc > bs) { bs = sc; pi = i; } });
      if (pi >= 0) {
        const totalCol = headers.findIndex((h, i) => /^(grand\s*)?total|jumla/i.test(h) && !periodCols0.some((c) => c.i === i));
        return { map: { product: pi }, how: { product: 'heading' }, wide: { product: pi, cols: periodCols0.map((c) => ({ i: c.i, p: c.p })), totalCol: totalCol >= 0 ? totalCol : null }, confident: true, layout: 'wide' };
      }
    }
    for (const k of order) {
      let best = -1;
      const pool = k === 'product' ? [(h) => STRONG_PRODUCT.test(h) && !NOT_PRODUCT.test(h), (h) => SYN.product.test(h) && !NOT_PRODUCT.test(h)] : [(h) => SYN[k].test(h)];
      for (const test of pool) for (let i = 0; i < headers.length && best < 0; i++) {
        if (used.has(i) || !test(headers[i])) continue;
        if (k === 'amount' && /price|rate|each|unit/i.test(headers[i]) && !/total/i.test(headers[i])) continue;
        if (k === 'product' && /\b(date|qty|quantity|amount|total|price|receipt|invoice|stock|balance)\b/i.test(headers[i])) continue;
        if (k === 'date' && stats[i].numeric === stats[i].n && stats[i].n > 0 && !/date|tarehe/i.test(headers[i])) continue;   // "Day" holding 1..31
        if (['qty', 'price', 'amount', 'stock'].includes(k) && stats[i].n && stats[i].numeric / stats[i].n < 0.5) continue;
        if (k === 'product' && stats[i].n && stats[i].numeric / stats[i].n > 0.8) continue;
        best = i;
      }
      if (best >= 0) take(k, best, 'heading');
    }
    // by content when the headings do not say
    if (map.product == null) {
      let bi = -1, bs = 0;
      headers.forEach((_, i) => { if (used.has(i)) return; const s = stats[i]; if (!s.n || s.numeric / s.n > 0.3 || s.dates) return; const sc = Math.min(s.distinct, 200) * (s.avgLen > 2 ? 1 : 0.3); if (sc > bs) { bs = sc; bi = i; } });
      if (bi >= 0 && bs >= 3) take('product', bi, 'content');
    }
    if (map.amount == null && !(map.qty != null && map.price != null)) {
      let bi = -1, bs = 0;
      headers.forEach((_, i) => { if (used.has(i)) return; const s = stats[i]; if (!s.n || s.numeric / s.n < 0.7 || /^(no\.?|#|id|sn|s\/n)$/i.test(headers[i])) return; if (s.sum > bs) { bs = s.sum; bi = i; } });
      if (bi >= 0) take('amount', bi, 'content');
    }
    if (map.date == null) {
      const di = headers.findIndex((_, i) => !used.has(i) && stats[i].n && stats[i].dates / stats[i].n > 0.6);
      if (di >= 0) take('date', di, 'content');
      else {
        for (let i = 0; i < headers.length; i++) { if (used.has(i) || !stats[i].n || stats[i].numeric / stats[i].n > 0.5) continue; const ok = sample.slice(0, 200).filter((r) => !isBlank(r[i]) && toDate(r[i], 'dmy')).length; if (ok / Math.max(1, Math.min(stats[i].n, 200)) > 0.8) { take('date', i, 'content'); break; } }
      }
    }
    const longOk = map.product != null && (map.amount != null || (map.qty != null && map.price != null));
    const wide = null;
    const confident = wide ? true : longOk;
    return { map, how, wide, confident, layout: wide ? 'wide' : 'long' };
  }

  /** A column heading that names a month or a date -> the first day of that period; otherwise null. */
  function periodOf(h) {
    const s = txt(h); if (!s) return null;
    if (/^(grand\s*)?total|jumla|avg|average|mean|%|share|qty|units|price/i.test(s)) return null;
    const d = toDate(s, 'dmy');
    if (d && /[A-Za-z]/.test(s) || (d && /^\d{4}[-\/]\d{1,2}$/.test(s))) return d;
    const m = s.match(/^([A-Za-z]{3,9})\.?(?:[-\/ ']*(\d{2,4}))?$/);
    if (m && monthIndex(m[1]) >= 0) { let y = m[2] ? +m[2] : null; if (y != null && y < 100) y += 2000; return { month: monthIndex(m[1]), year: y }; }
    return null;
  }

  /* ------------------------------------------------------------------ the analysis */
  // a line that only SUMS other lines. Written strictly so that a product such as "Total Energies" or "Sub-zero fridge" is never mistaken for one.
  const TOTAL_ROW = /^\s*(((grand|sub|running|daily|monthly|weekly|page|overall|net|gross)[\s-]*)?totals?(\s+(sales|amount|value|for\b.*|kes|ksh|usd))?|balance\s*(b\/?f|c\/?f|brought forward|carried forward)|brought forward|carried forward|b\/f|c\/f|jumla(\s*kuu)?|summary|sum)\s*[:.\-=]*\s*$/i;

  const normName = (s) => txt(s).normalize('NFKD').replace(/[̀-ͯ]/g, '').replace(/[“”"']/g, '').replace(/\s+/g, ' ').replace(/[\s.,;:]+$/g, '').toLowerCase();

  /**
   * table: { headers, rows } ; map/wide from guess() (or the person's own choice) ; opts: { currency, unit, order, sheetNames }
   * Returns the report data plus `recon` (the accounting of every row and every number) and `quality` notes.
   */
  function analyse(table, choice, opts) {
    opts = opts || {};
    const map = choice.map || {}, wide = choice.wide || null;
    const rowsIn = table.rows;
    const out = { rowsRead: rowsIn.length, buckets: {}, notes: [], examples: {} };
    const bucket = (k, amt, ex) => { const b = out.buckets[k] || (out.buckets[k] = { rows: 0, amount: 0 }); b.rows++; if (amt && isFinite(amt)) b.amount += amt; if (ex) { const e = out.examples[k] || (out.examples[k] = []); if (e.length < 4) e.push(ex); } };
    const order = opts.order || (map.date != null ? dateOrder(rowsIn.map((r) => r[map.date])) : 'dmy');
    out.dateOrder = order;
    const lines = [];   // every interpreted line, in file order (this is what the cleaned CSV lists)
    let derivedSum = 0, derivedRows = 0;   // amounts worked out as quantity x price where the amount cell was empty
    let readSum = 0;    // the sum of every number found in the amount cells: the figure everything must add up to
    const cur = { kes: 0, usd: 0, other: 0 };

    const addLine = (ln) => lines.push(ln);
    const productOf = (raw) => { const n = txt(raw); return n; };

    if (wide) {   // ---- months across the top: unpivot, then everything below is the same
      const yearGuess = opts.defaultYear || new Date().getFullYear();
      rowsIn.forEach((r, idx) => {
        const name = txt(r[wide.product]);
        const cells = wide.cols.map((c) => ({ c, v: r[c.i] }));
        const any = cells.some((x) => !isBlank(x.v)) || !isBlank(name);
        if (!any) { bucket('blank'); return; }
        if (name && TOTAL_ROW.test(name)) {
          let t = 0; cells.forEach((x) => { const n = num(x.v); if (!isNaN(n)) { t += n; readSum += n; } });
          bucket('total', t, `${name}`); addLine({ row: idx + 1, product: name, amount: t, type: 'total row (left out)' }); return;
        }
        let rowSum = 0, rowHas = false;
        cells.forEach((x) => {
          if (isBlank(x.v)) return;
          const n = num(x.v);
          if (isNaN(n)) { bucket('unreadable', 0, `${name || 'row ' + (idx + 1)}: "${txt(x.v)}"`); addLine({ row: idx + 1, product: name, amount: '', type: 'unreadable', note: txt(x.v) }); return; }
          readSum += n; rowSum += n; rowHas = true;
          const p = x.c.p, d = p instanceof Date ? p : new Date(p.year || (opts.sheetYear || yearGuess), p.month, 1);
          if (!name) { bucket('unnamed', n); }
          const kind = n > 0 ? 'sale' : n < 0 ? 'return' : 'zero';
          if (n === 0) { bucket('zero'); } else bucket(n > 0 ? 'sale' : 'return', n);
          addLine({ row: idx + 1, date: d, product: name || '(no product name)', qty: null, amount: n, type: kind });
        });
        if (wide.totalCol != null && rowHas && !isBlank(r[wide.totalCol])) { const t = num(r[wide.totalCol]); if (!isNaN(t) && Math.abs(t - rowSum) > Math.max(1, Math.abs(rowSum) * 0.005)) { out.totalMismatch = (out.totalMismatch || 0) + 1; (out.examples.totalMismatch = out.examples.totalMismatch || []).length < 3 && out.examples.totalMismatch.push(`${name}: months add to ${fmtN(rowSum)}, total column says ${fmtN(t)}`); } }
      });
      if (wide.totalCol != null) out.notes.push('Your Total column was used only to double-check the months; it is not added again.');
      out.wideUnit = true;
    } else {
      const hdr = table.headers.map((h) => h.toLowerCase());
      let runSum = 0, allSum = 0;
      rowsIn.forEach((r, idx) => {
        const rowNo = idx + 1;
        const cells = r.filter((c) => !isBlank(c));
        if (!cells.length) { bucket('blank'); return; }
        // a heading line repeated on every printed page
        const same = r.reduce((a, c, i) => a + (hdr[i] && txt(c).toLowerCase() === hdr[i] ? 1 : 0), 0);
        if (same >= Math.max(2, Math.ceil(cells.length * 0.6))) { bucket('heading'); addLine({ row: rowNo, type: 'repeated heading (skipped)' }); return; }
        const rawName = map.product != null ? txt(r[map.product]) : '';
        const qty = map.qty != null ? num(r[map.qty]) : NaN;
        const price = map.price != null ? num(r[map.price]) : NaN;
        const cell = map.amount != null ? r[map.amount] : undefined;
        let amount = map.amount != null ? num(cell) : NaN;
        let derived = false;
        if (isNaN(amount) && !isNaN(qty) && !isNaN(price) && (map.amount == null || isBlank(cell))) { amount = qty * price; derived = true; derivedSum += amount; derivedRows++; }
        const stock = map.stock != null ? num(r[map.stock]) : NaN;
        const rec = txt(map.receipt != null ? r[map.receipt] : '');
        const dRaw = map.date != null ? r[map.date] : null;
        const date = dRaw != null && !isBlank(dRaw) ? toDate(dRaw, order) : null;
        const hasNumber = !isNaN(amount);
        if (hasNumber) { readSum += amount; }
        // currency hints
        const cs = map.amount != null ? txt(cell) : ''; if (/kes|ksh|\/=/i.test(cs)) cur.kes++; else if (/\$|usd/i.test(cs)) cur.usd++; else if (cs) cur.other++;
        const base = { row: rowNo, date, product: rawName, qty: isNaN(qty) ? null : qty, price: isNaN(price) ? null : price, amount: hasNumber ? amount : '', stock: isNaN(stock) ? null : stock, receipt: rec };
        // a total or subtotal line: counted, shown, but not added in again
        const totalWord = !rawName && r.some((c) => typeof c === 'string' && TOTAL_ROW.test(c));
        // a nameless, dateless row of bare numbers is a total only if it really equals what came above it; otherwise it is a sale with no name
        const bare = map.product != null && !rawName && !date && !rec && hasNumber && cells.every((c) => looksNumeric(c));
        const sumsAbove = bare && [runSum, allSum].some((x) => Math.abs(x) > 0 && Math.abs(amount - x) <= Math.max(1, Math.abs(x) * 0.005));
        if ((rawName && TOTAL_ROW.test(rawName)) || totalWord || sumsAbove) {
          runSum = 0; bucket('total', hasNumber ? amount : 0, rawName || `row ${rowNo}`); addLine(Object.assign(base, { type: 'total row (left out)' })); return;
        }
        if (!hasNumber) {
          const shown = map.amount != null ? txt(cell) : '';
          if (shown) { bucket('unreadable', 0, `${rawName || 'row ' + rowNo}: "${shown}"`); addLine(Object.assign(base, { type: 'unreadable amount', note: shown })); }
          else { bucket('noamount'); addLine(Object.assign(base, { type: 'no amount' })); }
          return;
        }
        if (!rawName) { bucket('unnamed', amount); base.product = '(no product name)'; }
        runSum += amount; allSum += amount;
        if (amount === 0) { bucket('zero'); addLine(Object.assign(base, { type: 'zero value' })); return; }
        if (amount > 0) { bucket('sale', amount); addLine(Object.assign(base, { type: derived ? 'sale (qty x price)' : 'sale' })); }
        else { bucket('return', amount); addLine(Object.assign(base, { type: 'return / credit' })); }
        if (map.date != null && !date) bucket('nodate');
      });
    }

    /* ---- reconciliation: every number accounted for */
    const sale = (out.buckets.sale || {}).amount || 0, ret = (out.buckets.return || {}).amount || 0, tot = (out.buckets.total || {}).amount || 0;
    out.recon = { derived: derivedSum, derivedRows, readSum, sales: sale, returns: ret, totalRows: tot, net: sale + ret, diff: readSum - (sale + ret + tot) };
    out.recon.ok = Math.abs(out.recon.diff) < 0.005 * Math.max(1, Math.abs(readSum)) + 0.01;

    /* ---- the figures that matter, from sale and return lines */
    const live = lines.filter((l) => l.type.startsWith('sale') || l.type.startsWith('return') || l.type === 'zero value');
    const prods = new Map(); let spellingMerged = 0;
    for (const l of live) {
      const key = normName(l.product); let p = prods.get(key);
      if (!p) { p = { name: l.product, amount: 0, sales: 0, returns: 0, qty: 0, hasQty: false, last: null, first: null, stock: NaN, stockAt: null, spell: new Set() }; prods.set(key, p); }
      if (p.spell.size < 6) p.spell.add(l.product);
      p.amount += l.amount; if (l.amount > 0) p.sales += l.amount; else p.returns += l.amount;
      if (l.qty != null) { p.qty += l.amount < 0 && l.qty > 0 ? -l.qty : l.qty; p.hasQty = true; }
      if (l.date) { if (!p.last || l.date > p.last) p.last = l.date; if (!p.first || l.date < p.first) p.first = l.date; }
      if (l.stock != null) { if (l.date) { if (!p.stockAt || l.date >= p.stockAt) { p.stock = l.stock; p.stockAt = l.date; } } else p.stock = l.stock; }
    }
    const merged = [...prods.values()].filter((p) => p.spell.size > 1);
    out.merged = { count: merged.length, examples: merged.slice(0, 4).map((p) => [...p.spell].slice(0, 3).map((x) => `"${x}"`).join(' = ')) };
    const list = [...prods.values()].sort((a, b) => b.amount - a.amount);
    const dates = live.map((l) => l.date).filter(Boolean).sort((a, b) => a - b);
    out.lines = lines; out.live = live; out.list = list;
    out.first = dates[0] || null; out.last = dates[dates.length - 1] || null;
    out.netSales = out.recon.net; out.grossSales = sale; out.returns = ret;
    out.currency = opts.currency || (cur.usd > cur.kes ? 'USD' : 'KES');
    out.unit = opts.unit || (wide && opts.valueKind === 'qty' ? 'units' : null);

    // possible double entries: flagged, never removed
    const seenLine = new Map(); let dup = 0;
    for (const l of live) { if (!l.receipt) continue; const k = [l.receipt, normName(l.product), l.qty, l.amount, l.date ? +l.date : ''].join('|'); if (seenLine.has(k)) dup++; else seenLine.set(k, 1); }
    out.possibleDuplicates = dup;

    out.finds = []; analyseMore(out, live, list);
    return out;
  }

  const fmtN = (n) => Math.round(n).toLocaleString('en-KE');

  function analyseMore(out, live, list) {
    const total = out.netSales; const money = (n) => (out.unit ? fmtN(n) + ' ' + out.unit : out.currency + ' ' + fmtN(n));
    out.money = money;
    if (!list.length) return;
    let acc = 0, n80 = 0; const pos = list.filter((p) => p.amount > 0), posTotal = pos.reduce((a, p) => a + p.amount, 0);
    for (const p of pos) { acc += p.amount; n80++; if (acc >= posTotal * 0.8) break; }
    out.n80 = n80;
    if (pos.length >= 5) out.finds.push(`${n80} of your ${pos.length} products (${Math.round(n80 / pos.length * 100)}%) bring in 80% of your sales. Those are the ones to never run out of.`);
    const withDates = live.filter((l) => l.date);
    if (out.last && withDates.length) {
      const day = 86400000, span = Math.max(1, (out.last - out.first) / day + 1);
      out.span = span;
      out.slow = list.filter((p) => p.last && p.amount > 0 && (out.last - p.last) / day >= 30 && span >= 60)
        .map((p) => Object.assign({}, p, { quiet: Math.floor((out.last - p.last) / day) })).sort((a, b) => b.amount - a.amount).slice(0, 8);
      if (out.slow.length) out.finds.push(`${out.slow.length} product${out.slow.length > 1 ? 's that used to sell have' : ' that used to sell has'} not sold for 30 days or more, led by ${out.slow[0].name}. Check whether they are still on your shelf, and what they are costing you.`);
      let tied = 0, count = 0;
      for (const p of list) {
        if (!(p.stock > 0) || !p.hasQty || !(p.qty > 0) || !(p.sales > 0)) continue;
        const unit = p.sales / Math.max(1, p.qty), perDay = p.qty / span, cover = p.stock / perDay, quiet = p.last ? (out.last - p.last) / day : 0;
        if (cover >= 90 || quiet >= 30) { tied += p.stock * unit; count++; }
      }
      out.tied = { value: tied, count };
      if (count) out.finds.push(`About ${money(tied)} is tied up in ${count} slow-moving product${count > 1 ? 's' : ''} (stock that would take 90 days or more to sell at the current pace, or has not moved in a month). This is an estimate at selling price.`);
      const mon = new Map(), wk = [0, 0, 0, 0, 0, 0, 0], days = new Map();
      for (const l of withDates) {
        const k = l.date.getFullYear() + '-' + String(l.date.getMonth() + 1).padStart(2, '0');
        mon.set(k, (mon.get(k) || 0) + l.amount); wk[(l.date.getDay() + 6) % 7] += l.amount;
        const dk = k + '-' + l.date.getDate(); days.set(dk, 1);
      }
      out.months = [...mon.entries()].sort(); out.wk = wk; out.dayNames = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
      out.fullMonths = out.months.filter(([k], i) => {
        if (out.wideUnit) return true;   // month columns are whole months by nature
        const [y, m] = k.split('-').map(Number), len = new Date(y, m, 0).getDate();
        if (i === 0 && out.first.getDate() > 3) return false;
        if (i === out.months.length - 1 && out.last.getDate() < len - 3) return false;
        return true;
      });
      if (out.fullMonths.length >= 3) {
        const best = out.fullMonths.reduce((a, b) => (b[1] > a[1] ? b : a)), worst = out.fullMonths.reduce((a, b) => (b[1] < a[1] ? b : a));
        out.finds.push(`${monthName(best[0])} was your strongest full month (${money(best[1])}) and ${monthName(worst[0])} your quietest (${money(worst[1])}). Plan stock and cash for both.`);
      }
      if (!out.wideUnit && wk.filter((x) => x > 0).length >= 4 && days.size >= 14) {
        const bi = wk.indexOf(Math.max(...wk)); out.finds.push(`${out.dayNames[bi]} is your busiest day, with ${Math.round(wk[bi] / Math.max(1, wk.reduce((a, b) => a + (b > 0 ? b : 0), 0)) * 100)}% of sales.`);
      }
    }
    // what sells together (needs a receipt number)
    const rc = live.filter((l) => l.receipt && l.amount > 0);
    if (rc.length) {
      const bills = new Map();
      for (const l of rc) { const k = normName(l.product); let s = bills.get(l.receipt); if (!s) bills.set(l.receipt, (s = new Set())); s.add(k); }
      const names = new Map(list.map((p) => [normName(p.name), p.name]));
      const pairs = new Map(); let multi = 0;
      for (const s of bills.values()) { const a = [...s].slice(0, 25); if (a.length > 1) multi++; for (let i = 0; i < a.length; i++) for (let j = i + 1; j < a.length; j++) { const k = a[i] < a[j] ? a[i] + '\u0001' + a[j] : a[j] + '\u0001' + a[i]; pairs.set(k, (pairs.get(k) || 0) + 1); } }
      out.pairs = [...pairs.entries()].filter(([, c]) => c >= 3).sort((a, b) => b[1] - a[1]).slice(0, 6).map(([k, c]) => { const [a, b] = k.split('\u0001'); return { a: names.get(a) || a, b: names.get(b) || b, c }; });
      out.bills = bills.size; out.multi = multi;
      if (out.pairs.length) out.finds.push(`${out.pairs[0].a} and ${out.pairs[0].b} were bought together ${out.pairs[0].c} times. Keep them side by side and make sure you never run out of one of them.`);
    }
  }

  const MN = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
  function monthName(k) { const [y, m] = k.split('-').map(Number); return MN[m - 1] + ' ' + y; }

  /* ------------------------------------------------------------------ sheets: choose, combine */
  const headerKey = (t) => t.headers.map((h) => h.toLowerCase().replace(/[^a-z0-9]/g, '')).filter(Boolean);

  /** One table from a workbook: either the sheets that share the same columns put together (monthly sheets), or one sheet. */
  function buildTable(sheets, choice) {
    const tables = sheets.map((s) => { try { const t = findTable(s.rows); t.sheet = s.name; return t; } catch (e) { return null; } }).filter(Boolean);
    if (!tables.length) throw new Error('We could not find a table of rows and columns in that file.');
    const score = (t) => t.rows.reduce((a, r) => a + r.filter((c) => looksNumeric(c)).length, 0);
    let best = tables.slice().sort((a, b) => score(b) - score(a))[0];
    const like = tables.filter((t) => { const a = new Set(headerKey(best)), b = headerKey(t); const common = b.filter((x) => a.has(x)).length; return common / Math.max(a.size, b.length, 1) >= 0.8; });
    const info = { sheets: tables.map((t) => t.sheet), combined: false, used: [best.sheet] };
    if (choice && choice !== 'auto' && choice !== 'combine') { const t = tables.find((x) => x.sheet === choice); if (t) { best = t; info.used = [t.sheet]; } return Object.assign({}, best, { info }); }
    if (like.length > 1 && choice !== 'one') {
      const idx = new Map(headerKey(best).map((h, i) => [h, i]));
      const rows = []; const sheetOf = [];
      for (const t of like) {
        const hk = t.headers.map((h) => h.toLowerCase().replace(/[^a-z0-9]/g, ''));
        for (const r of t.rows) { const nr = Array(best.headers.length).fill(''); hk.forEach((h, i) => { if (idx.has(h) && i < r.length) nr[idx.get(h)] = r[i]; }); rows.push(nr); sheetOf.push(t.sheet); }
      }
      info.combined = true; info.used = like.map((t) => t.sheet);
      return { headers: best.headers, rows, titleRows: best.titleRows, firstDataRow: best.firstDataRow, sheetOf, info, sheet: 'all' };
    }
    return Object.assign({}, best, { info });
  }

  /** When there is no date column but the sheets are named after months, the sheet name is the month. */
  function datesFromSheetNames(table, map) {
    if (!table.sheetOf || map.date != null) return null;
    const names = [...new Set(table.sheetOf)];
    if (!names.every((n) => periodOf(n))) return null;
    const col = table.headers.length;
    const years = new Date().getFullYear();
    table.rows.forEach((r, i) => { const p = periodOf(table.sheetOf[i]); r[col] = p instanceof Date ? p : new Date(p.year || years, p.month, 1); });
    table.headers.push('Month (from sheet name)');
    return col;
  }

  /* ------------------------------------------------------------------ the cleaned file */
  function csvCell(v) { let s = v == null ? '' : v instanceof Date ? v.getFullYear() + '-' + String(v.getMonth() + 1).padStart(2, '0') + '-' + String(v.getDate()).padStart(2, '0') : String(v); if (/^[=+\-@]/.test(s) && isNaN(Number(s))) s = "'" + s; return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; }
  function cleanedCSV(out) {
    const head = ['Original row', 'Date', 'Receipt', 'Product', 'Quantity', 'Unit price', 'Amount', 'Stock', 'What we did with this row'];
    const body = out.lines.map((l) => [l.row, l.date || '', l.receipt || '', l.product || '', l.qty == null ? '' : l.qty, l.price == null ? '' : l.price, l.amount === '' || l.amount == null ? '' : l.amount, l.stock == null ? '' : l.stock, l.type + (l.note ? ': ' + l.note : '')].map(csvCell).join(','));
    return '﻿' + [head.join(','), ...body].join('\r\n');
  }

  return { readFile, decodeBytes, parseCSV, findTable, guess, analyse, buildTable, datesFromSheetNames, cleanedCSV, num, toDate, dateOrder, normName, periodOf, monthName, fmtN, TOTAL_ROW };
});
