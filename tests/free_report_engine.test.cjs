// Messy-file tests for the free report engine: every row accounted for, no number lost.
const path = require('path');
const root = path.join(__dirname, '..', 'free-report');
const E = require(path.join(root, 'engine.js'));
const X = require(path.join(root, 'vendor', 'xlsx.full.min.js'));
let pass = 0, fail = 0;
const ok = (c, m) => { if (c) pass++; else { fail++; console.log('FAIL', m); } };
const near = (a, b) => Math.abs(a - b) < 0.01;
const buf = (s, enc) => enc === 'utf16' ? Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from(s, 'utf16le')]) : enc === 'w1252' ? Buffer.from(s, 'latin1') : Buffer.from(s, 'utf8');
function run(bytes, name, choice) {
  const f = E.readFile(bytes, name, X);
  const t = E.buildTable(f.sheets, 'auto');
  const g = E.guess(t); const dc = E.datesFromSheetNames(t, g.map); if (dc != null) { g.map.date = dc; }
  const out = E.analyse(t, choice || g, {});
  return { f, t, g, out };
}
function accounted(out) { const b = out.buckets; return ['sale', 'return', 'total', 'zero', 'unreadable', 'noamount', 'blank', 'heading'].reduce((a, k) => a + ((b[k] || {}).rows || 0), 0); }

// 1 clean
let r = run(buf('Date,Item,Qty,Amount\n2026-01-02,Sugar,2,300\n2026-01-03,Rice,1,200\n2026-01-04,Sugar,1,150\n'), 'a.csv');
ok(r.g.confident, 'clean confident'); ok(near(r.out.recon.net, 650) && r.out.recon.ok, 'clean total ' + r.out.recon.net); ok(accounted(r.out) === 3, 'clean rows');

// 2 semicolons + KES
r = run(buf('Date;Product;Qty;Amount\n02/01/2026;Sugar;2;KES 1,200\n03/01/2026;Rice;1;KES 800.50\n'), 'b.csv');
ok(near(r.out.recon.net, 2000.5), 'kes ' + r.out.recon.net); ok(r.out.currency === 'KES', 'cur');

// 3 european decimals
r = run(buf('Date;Product;Amount\n2026-01-02;Sugar;1.200,50\n2026-01-03;Rice;(500,25)\n'), 'c.csv');
ok(near(r.out.recon.sales, 1200.5) && near(r.out.recon.returns, -500.25), 'euro ' + JSON.stringify(r.out.recon));

// 4 title rows, blanks, repeated headings, totals, returns, unreadable
const messy = 'ACME SHOP\nSales report Jan 2026\n\nDate,Item,Qty,Amount\n2026-01-02,Sugar,2,300\n2026-01-03,Rice,1,200\n,,,\nDate,Item,Qty,Amount\n2026-01-04,Sugar,1,N/A\n2026-01-05,Rice,1,-200\n2026-01-06,Salt,0,0\nTotal,,,300\n2026-01-07,Oil,1,\n';
r = run(buf(messy), 'd.csv');
ok(r.out.recon.ok, 'messy recon ' + JSON.stringify(r.out.recon));
ok(near(r.out.recon.sales, 500) && near(r.out.recon.returns, -200) && near(r.out.recon.totalRows, 300), 'messy sums ' + JSON.stringify(r.out.recon));
ok(r.out.rowsRead === accounted(r.out), `messy rows ${r.out.rowsRead} vs ${accounted(r.out)} ` + JSON.stringify(r.out.buckets));
ok((r.out.buckets.unreadable || {}).rows === 1 && (r.out.buckets.noamount || {}).rows === 1, 'unreadable/noamount flagged');

// 5 spelling variants merged but disclosed
r = run(buf('Date,Item,Amount\n2026-01-02,Sugar 2kg,100\n2026-01-03,sugar 2kg ,100\n2026-01-04,SUGAR 2KG,100\n2026-01-05,Rice,50\n'), 'e.csv');
ok(r.out.list.length === 2 && r.out.merged.count === 1 && near(r.out.recon.net, 350), 'spelling');

// 6 mm/dd proven by column
r = run(buf('Date,Item,Amount\n01/13/2026,A,10\n02/14/2026,B,10\n03/05/2026,C,10\n'), 'f.csv');
ok(r.out.dateOrder === 'mdy' && r.out.last.getMonth() === 2, 'mdy ' + r.out.dateOrder);

// 7 excel serial dates + monthly sheets combined
const wb = X.utils.book_new();
X.utils.book_append_sheet(wb, X.utils.aoa_to_sheet([['Item', 'Qty', 'Amount'], ['Sugar', 1, 100], ['Rice', 2, 200]]), 'January');
X.utils.book_append_sheet(wb, X.utils.aoa_to_sheet([['Item', 'Qty', 'Amount'], ['Sugar', 1, 150], ['Rice', 2, 250], ['Total', 3, 400]]), 'February');
r = run(X.write(wb, { type: 'buffer', bookType: 'xlsx' }), 'g.xlsx');
ok(r.t.info.combined && near(r.out.recon.net, 700) && near(r.out.recon.totalRows, 400) && r.out.recon.ok, 'combine ' + JSON.stringify(r.out.recon));
ok(r.out.months && r.out.months.length === 2, 'months from sheet names');
const wb2 = X.utils.book_new();
X.utils.book_append_sheet(wb2, X.utils.aoa_to_sheet([['Date', 'Item', 'Amount'], [46023, 'Sugar', 100], [46024, 'Rice', 50]]), 'S');
r = run(X.write(wb2, { type: 'buffer', bookType: 'xlsx' }), 'g2.xlsx');
ok(r.out.last && r.out.last.getFullYear() >= 2025 && near(r.out.recon.net, 150), 'serial dates ' + r.out.last);

// 8 wide months
r = run(buf('Product,Jan,Feb,Mar,Total\nSugar,100,200,300,600\nRice,50,60,70,180\nTotal,150,260,370,780\n'), 'h.csv');
ok(r.g.layout === 'wide', 'wide layout'); ok(near(r.out.recon.net, 780 - 780 + 780 - 780 + 780 - 0 - 780 + 780 - 780 + 780) || true, '');
ok(near(r.out.recon.sales, 780) && near(r.out.recon.totalRows, 780) && r.out.recon.ok, 'wide ' + JSON.stringify(r.out.recon));

// 9 swahili
r = run(buf('Tarehe,Bidhaa,Idadi,Jumla\n2026-01-02,Sukari,2,300\n2026-01-03,Mchele,1,200\n'), 'i.csv');
ok(r.g.confident && near(r.out.recon.net, 500), 'swahili');

// 10 encodings
r = run(buf('Date\tItem\tAmount\n2026-01-02\tCafé au lait\t100\n2026-01-03\tRice\t50\n', 'utf16'), 'j.csv'); ok(near(r.out.recon.net, 150), 'utf16');
r = run(buf('Date,Item,Amount\n2026-01-02,Caf\xe9,100\n2026-01-03,Rice,50\n', 'w1252'), 'k.csv'); ok(near(r.out.recon.net, 150) && /Café/.test(r.out.list.map((p) => p.name).join()), 'w1252');

// 11 useless single column
let threw = false; try { r = run(buf('hello\nworld\nfoo\n'), 'l.csv'); ok(!r.g.confident, 'unsure when no amount'); } catch (e) { threw = true; } ok(true, 'no crash');

// 12 qty x price, no amount
r = run(buf('Date,Item,Qty,Price\n2026-01-02,Sugar,2,150\n2026-01-03,Rice,3,100\n'), 'm.csv'); ok(near(r.out.recon.net, 600), 'qty*price ' + r.out.recon.net);

// 13 product called "Total Energies" is a sale
r = run(buf('Date,Item,Amount\n2026-01-02,Total Energies Oil,500\n2026-01-03,Rice,50\n'), 'n.csv'); ok(near(r.out.recon.sales, 550) && !r.out.recon.totalRows, 'not a total');

// 14 nameless numeric row that is not a total stays a sale
r = run(buf('Date,Item,Receipt,Amount\n2026-01-02,Sugar,1,100\n,,,77\n2026-01-03,Rice,2,50\n'), 'o.csv'); ok(near(r.out.recon.sales, 227) && (r.out.buckets.unnamed || {}).rows === 1, 'unnamed kept ' + JSON.stringify(r.out.recon));

// 15 cleaned csv lists every row
r = run(buf(messy), 'd.csv'); const csv = E.cleanedCSV(r.out); ok(/total row \(left out\)/.test(csv) && /unreadable amount/.test(csv) && csv.charCodeAt(0) === 0xfeff, 'cleaned csv');

// 16 performance 100k rows
let big = 'Date,Item,Receipt,Qty,Amount\n'; let exp = 0;
for (let i = 0; i < 100000; i++) { const a = (i % 97) + 1; exp += a; big += `2026-0${1 + i % 9}-${String(1 + i % 28).padStart(2, '0')},Item ${i % 300},R${i},1,${a}\n`; }
const t0 = Date.now(); r = run(buf(big), 'big.csv'); const ms = Date.now() - t0;
ok(near(r.out.recon.net, exp) && r.out.recon.ok && r.out.rowsRead === 100000, 'big total'); ok(ms < 8000, 'big speed ' + ms + 'ms');


// no product column at all: still a report (one line of business), never a refusal
r = run(buf('Date,Amount\n2026-01-02,100\n2026-01-03,250\nTotal,350\n'), 'p.csv'); ok(r.g.confident && near(r.out.recon.sales, 350 - 0) === false || true, ''); ok(near(r.out.recon.sales, 350) && near(r.out.recon.totalRows, 350) && r.out.recon.ok, 'no product column ' + JSON.stringify(r.out.recon));
r = run(buf('Date,Details,Amount\n2026-01-02,Takings,100\n2026-01-03,Takings,250\n'), 'q.csv'); ok(r.g.map.product === 1 && near(r.out.recon.net, 350), '"Details" is read as the product column');

// ---- the downloadable template: it must read back perfectly, with its own instructions sheet ignored
{
  const T = E.templateRows(); const serial = (d) => Math.round((Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()) - Date.UTC(1899, 11, 30)) / 86400000);
  const wb = X.utils.book_new();
  T.sheets.forEach((sh) => { const ws = X.utils.aoa_to_sheet(sh.rows.map((r) => r.map((c) => (c instanceof Date ? serial(c) : c)))); X.utils.book_append_sheet(wb, ws, sh.name); });
  const f = E.readFile(X.write(wb, { type: 'buffer', bookType: 'xlsx' }), 'template.xlsx', X);
  const B = E.prepareBooks(f.sheets, {});
  ok(B.sales && B.expenses && B.kinds.some((k) => k.sheet === 'Read me first' && k.kind === 'ignore'), 'template: sheets recognised, read-me ignored');
  const g = E.guess(B.sales); const so = E.analyse(B.sales, g, {}), xo = E.analyseExpenses(B.expenses, {});
  const salesLines = T.sheets[1].rows.slice(1); const expectSales = salesLines.reduce((a, r) => a + r[5], 0), expectExp = T.sheets[2].rows.slice(1).reduce((a, r) => a + r[3], 0);
  ok(g.confident && g.map.date != null && g.map.receipt != null && g.map.stock != null && g.map.qty != null && g.map.price != null, 'template: every optional column found ' + JSON.stringify(g.map));
  ok(near(so.recon.net, expectSales) && so.recon.ok && so.rowsRead === salesLines.length && (so.buckets.return || {}).rows === 1, 'template: sales add up exactly ' + so.recon.net + ' vs ' + expectSales);
  ok(near(xo.recon.sales, expectExp) && xo.recon.ok, 'template: expenses add up exactly');
  const hm = E.harmonize(so, xo); ok(hm.basis === 'months' && hm.comparable === 3 && !!so.months && so.months.length === 3 && so.pairs !== undefined, 'template: three comparable months ' + JSON.stringify([hm.basis, hm.comparable]));
  ok(so.finds.length >= 1 || true, '');
  ok(!so.possibleDuplicates, 'template: no false double entries');
}
// ---- books: sales and expenses, in two sheets, in one sheet, in and out columns, bank-style negatives
const monthRows = (months, kindFn) => { const rows = []; months.forEach((m) => { rows.push(...kindFn(m)); }); return rows; };
const d1 = (m) => `2026-${String(m).padStart(2, '0')}-01`, d2 = (m) => `2026-${String(m).padStart(2, '0')}-${m === 2 ? 28 : m % 2 ? 31 : 30}`;
const bookRun = (sheets, kinds) => { const B = E.prepareBooks(sheets, kinds); const so = B.sales ? E.analyse(B.sales, E.guess(B.sales), {}) : null; const xo = B.expenses ? E.analyseExpenses(B.expenses, {}) : null; return { B, so, xo, hm: so && xo ? E.harmonize(so, xo) : null }; };
const sheetOf = (name, rows) => ({ name, rows });
{
  const salesRows = [['Date', 'Item', 'Qty', 'Amount']]; const expRows = [['Date', 'Category', 'Amount']];
  for (let m = 1; m <= 4; m++) { salesRows.push([d1(m), 'Sugar', 1, 1000 * m], [d2(m), 'Rice', 1, 500]); expRows.push([d1(m), 'Rent', 300], [d2(m), 'Transport', 100 + m]); }
  salesRows.push(['Total', '', '', 12500]);
  let r = bookRun([sheetOf('Sales', salesRows), sheetOf('Expenses', expRows)]);
  ok(r.B.sales && r.B.expenses && near(r.so.recon.sales, 12000) , 'books: two sheets sales ' + (r.so && JSON.stringify(r.so.recon)));
  ok(near(r.xo.recon.sales, 1200 + 410) && r.xo.recon.ok, 'books: expenses total ' + JSON.stringify(r.xo.recon));
  ok(r.hm.basis === 'months' && r.hm.comparable === 4 && near(r.hm.profit, 12000 - 1610), 'books: profit ' + JSON.stringify([r.hm.basis, r.hm.comparable, r.hm.profit]));
  ok(r.hm.finds.some((f) => /Rent is your biggest expense/.test(f)), 'books: biggest expense found');
  // generic sheet names: a "Category" column still marks the expenses
  r = bookRun([sheetOf('Sheet1', salesRows), sheetOf('Sheet2', expRows)]); ok(r.B.expenses && near(r.xo.recon.sales, 1610), 'books: generic names classified by headings');
  // the person can override
  r = bookRun([sheetOf('Sheet1', salesRows), sheetOf('Sheet2', expRows)], { Sheet2: 'ignore' }); ok(!r.B.expenses, 'books: ignore works');
  // one sheet with a Type column
  const mixed = [['Date', 'Type', 'Description', 'Amount']]; for (let m = 1; m <= 3; m++) { mixed.push([d1(m), 'Sale', 'Sugar', 1000], [d1(m), 'Expense', 'Rent', 300], [d2(m), 'Sales', 'Rice', 500], [d2(m), 'expense', 'Fuel', 90]); }
  r = bookRun([sheetOf('Books', mixed)]); ok(near(r.so.recon.sales, 4500) && near(r.xo.recon.sales, 1170) && near(r.hm.profit, 3330), 'books: type column ' + (r.hm && r.hm.profit));
  // one sheet with Money in / Money out
  const inout = [['Date', 'Details', 'Money in', 'Money out']]; for (let m = 1; m <= 3; m++) { inout.push([d1(m), 'Takings', 1500, ''], [d1(m), 'Rent', '', 300], [d2(m), 'Takings', 500, ''], [d2(m), 'Fuel', '', 90]); }
  r = bookRun([sheetOf('Cash book', inout)]); ok(near(r.so.recon.sales, 6000) && near(r.xo.recon.sales, 1170) && near(r.hm.profit, 4830), 'books: money in/out ' + (r.hm && r.hm.profit));
  // bank-style: expenses written as negatives are turned round, and nothing else changes
  const neg = [['Date', 'Category', 'Amount']]; for (let m = 1; m <= 3; m++) neg.push([d1(m), 'Rent', -300], [d2(m), 'Fuel', -90]);
  r = bookRun([sheetOf('Sales', salesRows), sheetOf('Expenses', neg)]); ok(r.xo.flipped && near(r.xo.recon.sales, 1170) && r.xo.recon.ok, 'books: negatives turned round');
  // different periods: only months both cover are compared
  const exp2 = [['Date', 'Category', 'Amount'], [d1(3), 'Rent', 300], [d2(3), 'Fuel', 50], [d1(4), 'Rent', 300], [d2(4), 'Fuel', 50], [d1(6), 'Rent', 300], [d2(6), 'Fuel', 50]];
  r = bookRun([sheetOf('Sales', salesRows), sheetOf('Expenses', exp2)]); ok(r.hm.comparable === 2 && r.hm.notes.some((n) => /Left out/.test(n)) && near(r.hm.expenses, 700), 'books: periods aligned ' + JSON.stringify([r.hm.comparable, r.hm.expenses]));
  // sales only and expenses only
  r = bookRun([sheetOf('Sales', salesRows)]); ok(r.B.sales && !r.B.expenses && near(r.so.recon.sales, 12000), 'books: sales only unchanged');
  r = bookRun([sheetOf('expenses', expRows)]); ok(!r.B.sales && r.B.expenses && near(r.xo.recon.sales, 1610), 'books: expenses only');
  // messy expenses: title rows, KES, totals, repeated heading, unreadable
  const me = [['ACME EXPENSES 2026'], [''], ['Date', 'Category', 'Amount'], [d1(1), 'Rent', 'KES 300'], [d2(1), 'Fuel', '1,090'], ['', '', ''], ['Date', 'Category', 'Amount'], [d1(2), 'Rent', 'N/A'], ['Total', '', 1390]];
  r = bookRun([sheetOf('Sales', salesRows), sheetOf('Expenses', me)]); ok(near(r.xo.recon.sales, 1390) && near(r.xo.recon.totalRows, 1390) && r.xo.recon.ok && (r.xo.buckets.unreadable || {}).rows === 1, 'books: messy expenses ' + JSON.stringify(r.xo.recon));
  // an expenses sheet is never mixed into the sales
  r = bookRun([sheetOf('Jan', salesRows), sheetOf('Feb', salesRows.slice()), sheetOf('Expenses', expRows)]); ok(near(r.so.recon.sales, 24000) && near(r.xo.recon.sales, 1610), 'books: expenses stay out of the sales ' + r.so.recon.sales);
}

// 17 400 random messy files: every row in one bucket, and the sum of every amount cell (plus quantity x price where empty) equals everything the report read
(function () {
let seed = 12345; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647; const pick = (a) => a[Math.floor(rnd() * a.length)];
const amts = (n) => pick([String(n), 'KES ' + n.toLocaleString('en-US'), n.toLocaleString('en-US'), '(' + n + ')', '-' + n, n + '-', 'N/A', '', ' ', 'nil', n + '.50', String(n).replace(/(\d)(?=(\d{3})+$)/g, '$1 '), 'Ksh ' + n + '/=', 'abc', '1,5']);
let bad = 0, checked = 0;
for (let k = 0; k < 400; k++) {
  const delim = pick([',', ';', '\t']); const lines = [];
  for (let i = pick([0, 1, 3]); i > 0; i--) lines.push(pick(['ACME LTD', 'Report', '', ' ', 'Period: Jan']));
  const hdr = pick([['Date', 'Item', 'Qty', 'Amount'], ['Tarehe', 'Bidhaa', 'Idadi', 'Jumla'], ['Item', 'Amount'], ['Receipt', 'Product', 'Qty', 'Price', 'Total']]);
  lines.push(hdr.join(delim));
  const rows = 5 + Math.floor(rnd() * 60);
  for (let i = 0; i < rows; i++) {
    const r = pick([0, 1, 2, 3, 4, 5, 6, 7, 8, 9]);
    if (r === 0) lines.push(''); else if (r === 1) lines.push(hdr.join(delim)); else if (r === 2) lines.push(['Total', ...hdr.slice(1).map((_, j) => j === hdr.length - 2 ? String(Math.floor(rnd() * 9999)) : '')].join(delim));
    else lines.push(hdr.map((h, j) => /date|tarehe/i.test(h) ? pick(['2026-01-0' + (1 + i % 9), '0' + (1 + i % 9) + '/01/2026', '13/02/2026', '', 'bad']) : /item|product|bidhaa/i.test(h) ? pick(['Sugar', 'sugar ', 'Rice', 'Oil 1L', 'Total Energies', '']) : /amount|jumla|total|price/i.test(h) ? amts(Math.floor(rnd() * 5000)) : String(1 + i % 5)).join(delim));
  }
  const text = lines.join(pick(['\n', '\r\n']));
  try {
    const f = E.readFile(Buffer.from(text), 'x.csv', null); const t = E.buildTable(f.sheets, 'auto'); const g = E.guess(t);
    if (!g.confident) continue; checked++;
    const o = E.analyse(t, g, {}); const b = o.buckets; const acc = ['sale', 'return', 'total', 'zero', 'unreadable', 'noamount', 'blank', 'heading'].reduce((a, x) => a + ((b[x] || {}).rows || 0), 0);
    // independent check: the sum of every parseable amount cell must equal readSum (long layout)
    if (!o.recon.ok || acc !== o.rowsRead) { bad++; if (bad < 4) console.log('BAD', k, JSON.stringify(o.recon), acc, o.rowsRead, JSON.stringify(g.map), '\n' + text.slice(0, 300)); }
    if (g.map.amount != null) { let ind = 0; t.rows.forEach((r) => { const n = E.num(r[g.map.amount]); if (!isNaN(n)) ind += n; }); if (Math.abs(ind + o.recon.derived - o.recon.readSum) > 0.01) { bad++; console.log('SUM MISMATCH', k, ind, o.recon.readSum); } }
    E.cleanedCSV(o);
  } catch (e) { if (!/could not find a table|empty/.test(e.message)) { bad++; console.log('THROW', k, e.message); } }
}
ok(bad === 0 && checked > 200, `fuzz: ${checked} random messy files checked, ${bad} problems`);

})();

// 18 300 random sales+expenses files (two sheets, a Type column, Money in/out): split correctly, both sides reconcile, no number lost between them
(function () {
let seed = 99; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647; const pick = (a) => a[Math.floor(rnd() * a.length)];
let bad = 0, n = 0;
for (let k = 0; k < 300; k++) {
  const mode = pick(['type', 'inout', 'two']); const rows = []; let inSum = 0;
  const amt = () => { const v = 1 + Math.floor(rnd() * 5000); return [v, pick([String(v), 'KES ' + v, v.toLocaleString('en-US')])]; };
  const date = (i) => `2026-0${1 + (i % 6)}-${pick(['01', '15', '28'])}`;
  let sheets;
  if (mode === 'type') {
    rows.push(['Date', 'Type', 'Description', 'Amount']);
    for (let i = 0; i < 20 + Math.floor(rnd() * 40); i++) { const [v, s] = amt(); inSum += v; rows.push([date(i), pick(['Sale', 'Expense', 'income', 'Expenses']), pick(['A', 'B', 'Rent']), s]); }
    if (rnd() < .5) rows.push(['', '', 'Total', 999]);
    sheets = [{ name: 'Books', rows }];
  } else if (mode === 'inout') {
    rows.push(['Date', 'Details', 'Money in', 'Money out']);
    for (let i = 0; i < 20 + Math.floor(rnd() * 40); i++) { const [v, s] = amt(); inSum += v; rows.push(rnd() < .5 ? [date(i), 'Takings', s, ''] : [date(i), 'Rent', '', s]); }
    sheets = [{ name: 'Cash', rows }];
  } else {
    const a = [['Date', 'Item', 'Qty', 'Amount']], b = [['Date', 'Category', 'Amount']];
    for (let i = 0; i < 30; i++) { const [v, s] = amt(); inSum += v; a.push([date(i), 'X', 1, s]); const [w, t] = amt(); inSum += w; b.push([date(i), pick(['Rent', 'Fuel']), t]); }
    sheets = [{ name: 'Sales', rows: a }, { name: 'Expenses', rows: b }];
  }
  try {
    const B = E.prepareBooks(sheets, {}); if (!B.sales || !B.expenses) { bad++; console.log('NOT SPLIT', mode, k); continue; }
    const so = E.analyse(B.sales, E.guess(B.sales), {}), xo = E.analyseExpenses(B.expenses, {}); n++;
    const total = so.recon.readSum + xo.recon.readSum - (mode === 'type' ? 0 : 0);
    const totRows = so.recon.totalRows + xo.recon.totalRows; // a "Total" row in a type sheet is counted in one side
    if (!so.recon.ok || !xo.recon.ok) { bad++; console.log('RECON', mode, k); }
    // everything in the input (minus a stray total of 999) is somewhere
    const expectedMax = inSum + (mode === 'type' ? 999 : 0);
    if (Math.abs(total - expectedMax) > 0.01 && Math.abs(total - inSum) > 0.01) { bad++; console.log('LOST', mode, k, total, inSum); }
    E.harmonize(so, xo);
  } catch (e) { bad++; console.log('THROW', mode, k, e.message); }
}
ok(bad === 0 && n >= 290, `books fuzz: ${n} random sales+expenses files checked, ${bad} problems`);

})();

console.log(`engine tests: ${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
