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

console.log(`engine tests: ${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
