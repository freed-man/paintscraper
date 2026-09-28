/*
 * BSP Peinture: export every paint code (make, code, name, finish, hex)
 * ======================================================================
 * 1. Open https://www.bsp-peinture.fr/trouver-code-couleur in Chrome, Edge or Firefox.
 * 2. Open the console: F12 (Mac: Cmd+Option+J), then the "Console" tab.
 *    The first time, the browser may ask you to type  allow pasting  and press Enter.
 * 3. Paste this whole file and press Enter.
 *    It loads each make's "Voir tous les codes" page in the background, logs its
 *    progress, and downloads bsp-paint-codes-YYYY-MM-DD.csv when it's done.
 *
 * Stopped halfway?  Paste it again in the same tab; makes already done are skipped.
 * bspDownload()        saves what has been collected so far as CSV
 * bspDownload('json')  same, as JSON
 * Reload the page to start over from scratch.
 */
(async () => {
  // ------------------------------- settings -------------------------------
  const INDEX_PATH  = '/trouver-code-couleur'; // or '/trouver-code-couleur-moto', '/trouver-code-couleur-camion'
  const ONLY        = [];   // e.g. ['rox', 'renault'] to try a few makes first (slug = end of the make's URL)
  const CONCURRENCY = 2;    // pages loaded at once
  const DELAY_MS    = 500;  // pause after each make, per worker, to keep the load on their server light
  const RETRY_MS    = 3000; // first wait after a failed page; doubles on each of 3 retries
  // -------------------------------------------------------------------------

  if (!/bsp-(peinture\.fr|lack\.de)$/.test(location.hostname)) {
    console.error(`Open www.bsp-peinture.fr first; this tab is on ${location.hostname}.`);
    return;
  }

  const S = (window.__bsp = window.__bsp || { byMake: {}, order: [], done: new Set(), warned: false, warnedEmpty: false });
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const bare = h => h.replace(/^www\./, '');
  const toUrl = (href, base) => {
    try { const u = new URL(href, base); return bare(u.hostname) === bare(location.hostname) ? u : null; }
    catch (e) { return null; }
  };
  const t0 = Date.now();

  // ---------- download (also callable any time as bspDownload()) ----------
  const COLS = ['make', 'code', 'name', 'finish', 'hex', 'swatch_css', 'url'];
  window.bspDownload = (fmt = 'csv') => {
    const rows = S.order.flatMap(slug => S.byMake[slug] || []);
    const json = fmt === 'json';
    const esc = v => { v = v == null ? '' : String(v); return /[",\r\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; };
    const text = json ? JSON.stringify(rows, null, 1)
      : '\ufeff' + [COLS.join(','), ...rows.map(r => COLS.map(c => esc(r[c])).join(','))].join('\r\n');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([text], { type: json ? 'application/json' : 'text/csv;charset=utf-8' }));
    a.download = `bsp-paint-codes-${new Date().toISOString().slice(0, 10)}.${json ? 'json' : 'csv'}`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 60000);
    return `${rows.length} rows saved to ${a.download}`;
  };

  // ---------- fetching: returns a Document, false for a 404, null if it kept failing ----------
  async function getDoc(path) {
    for (let attempt = 0; attempt < 4; attempt++) {
      if (attempt) await sleep(RETRY_MS * 2 ** (attempt - 1));
      try {
        const res = await fetch(path, { credentials: 'include' });
        if (res.ok) return new DOMParser().parseFromString(await res.text(), 'text/html');
        if (res.status === 404) return false;
        console.warn(`HTTP ${res.status} for ${path}${attempt < 3 ? ', retrying…' : ''}`);
      } catch (e) {
        console.warn(`Network error for ${path}${attempt < 3 ? ', retrying…' : ''}`, e.message);
      }
    }
    return null;
  }

  // ---------- text: "NAME | Code X | FINISH" -> { name, code, finish } ----------
  function textTokens(el) {
    const out = [], walker = el.ownerDocument.createTreeWalker(el, 4 /* text nodes */);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      if (/^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(n.parentElement ? n.parentElement.tagName : '')) continue;
      const t = clean(n.nodeValue);
      if (t) out.push(t);
    }
    return out;
  }

  const FINISH_RE = /\s+(Solide|Métallisé|Metallise|Nacré|Nacre|Tricouche|Bicouche|Multi-ton|Mat|Satiné|Texturé|Chromé)$/i;
  function parseText(toks) {
    toks = toks.filter(t => !/^(→|›|»|>|voir\b.*)$/i.test(t));
    let i = -1, name, code, after;
    for (let k = toks.length - 1; k >= 0; k--) if (/^code\b/i.test(toks[k])) { i = k; break; }
    if (i >= 0) {
      after = toks.slice(i + 1);
      code = toks[i].replace(/^code\s*:?\s*/i, '');
      if (!code) code = after.shift() || '';
      name = toks.slice(0, i).join(' ');
    } else {
      const m = toks.join(' ').match(/^(.*?)\s*\bCode\b\s*:?\s*(\S+)\s*(.*)$/i);
      if (!m) return null;
      [, name, code] = m;
      after = m[3] ? [m[3]] : [];
    }
    if (!name && after.length) name = after.shift();
    let finish = after.join(' ');
    const f = !finish && code.match(FINISH_RE);
    if (f) { finish = f[1]; code = code.slice(0, f.index); }
    return clean(code) ? { name: clean(name), code: clean(code), finish: clean(finish) } : null;
  }

  // ---------- colour: best swatch colour found in an element ----------
  const COLOUR_RE = /#(?:[0-9a-f]{8}|[0-9a-f]{6}|[0-9a-f]{3,4})(?![0-9a-z])|rgba?\([^)]*\)|hsla?\([^)]*\)/i;
  const hex2 = n => Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, '0');
  function toHex(c) {
    let m;
    c = c.trim().toLowerCase();
    if ((m = c.match(/^#([0-9a-f]{3,8})$/)) && [3, 4, 6, 8].includes(m[1].length))
      return '#' + (m[1].length < 6 ? [...m[1].slice(0, 3)].map(x => x + x).join('') : m[1].slice(0, 6)).toUpperCase();
    if ((m = c.match(/^rgba?\(\s*([\d.]+)(%?)[\s,]+([\d.]+)(%?)[\s,]+([\d.]+)(%?)/)))
      return '#' + [1, 3, 5].map(k => hex2(m[k + 1] ? m[k] * 2.55 : +m[k])).join('').toUpperCase();
    if ((m = c.match(/^hsla?\(\s*([\d.]+)(?:deg)?[\s,]+([\d.]+)%[\s,]+([\d.]+)%/))) {
      const h = m[1] / 30, s = m[2] / 100, l = m[3] / 100, a = s * Math.min(l, 1 - l);
      const f = n => { const k = (n + h) % 12; return 255 * (l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1))); };
      return '#' + [f(0), f(8), f(4)].map(hex2).join('').toUpperCase();
    }
    return null;
  }
  // priority: data-hex/colour attributes > background or CSS-variable styles > fill > colour in an image URL
  function findColour(root) {
    let best = null;
    const first = v => { const m = v.match(COLOUR_RE); return m ? m[0] : null; };
    const take = (score, colour, css) => {
      const hex = colour && toHex(colour);
      if (hex && (!best || score > best.score)) best = { score, hex, css: clean(css) };
    };
    for (const el of [root, ...root.querySelectorAll('*')]) {
      for (const { name, value } of el.attributes) {
        if (name === 'style') {
          for (const decl of value.split(';')) {
            const k = decl.indexOf(':');
            if (k < 0) continue;
            const prop = decl.slice(0, k).trim().toLowerCase(), val = decl.slice(k + 1);
            if (/^(background|--)/.test(prop)) take(4, first(val), val);
            else if (prop === 'fill' || prop === 'stop-color') take(3, first(val), val);
          }
        } else if (name.startsWith('data-')) {
          const b = /hex|rgb/.test(name) && value.trim().match(/^#?([0-9a-f]{6})$/i);
          take(/hex|rgb|colou?r|swatch/.test(name) ? 5 : 2, b ? '#' + b[1] : first(value), value);
        } else if (/^(fill|stop-color|bgcolor)$/.test(name)) {
          take(3, first(value), value);
        } else if (name === 'src') {
          const m = value.match(/[?&](?:hex|colou?r|couleur|c)=(?:%23|#)?([0-9a-f]{6})\b|[/_-]([0-9a-f]{6})\.(?:png|jpe?g|webp|gif|svg)\b/i);
          if (m) take(2, '#' + (m[1] || m[2]), value);
        }
      }
    }
    return best;
  }

  // the page's own figure, e.g. "4539 codes constructeurs référencés", used as a sanity check
  function expectedCount(d) {
    const w = d.createTreeWalker(d.body || d, 4);
    for (let n = w.nextNode(); n; n = w.nextNode()) {
      if (!/constructeurs?\s+r[ée]f[ée]renc/i.test(n.nodeValue)) continue;
      const m = (n.parentElement ? n.parentElement.textContent : n.nodeValue).match(/(\d[\d \u00a0\u202f.]*)\s*codes?\s+constructeurs?/i);
      if (m) return +m[1].replace(/\D/g, '');
    }
    return null;
  }

  // ---------- one make ----------
  async function scrapeMake(mk) {
    const prefix = mk.path + '/code/';
    let listPath = mk.path + '/tous-les-codes';
    let doc = await getDoc(listPath);
    if (doc === false) { listPath = mk.path; doc = await getDoc(listPath); } // no "all codes" page: use the make page
    if (!doc) return null;

    const rows = [], keys = new Set(), pages = [listPath], seen = new Set(pages);
    let expected = null, sample = '', excerpt = '';
    for (let p = 0; p < pages.length && p < 500; p++) {
      const d = p === 0 ? doc : await getDoc(pages[p]);
      if (!d) return null;
      const base = location.origin + pages[p];
      if (p === 0) {
        expected = expectedCount(d);
        const html = d.body ? d.body.innerHTML : '';
        const at = Math.max(0, html.search(/r[ée]f[ée]renc[ée]s/i));
        excerpt = html.slice(at, at + 3000);
      }

      // code links, plus which ancestors contain only one code (= that code's card)
      const links = [], owner = new Map();
      for (const a of d.querySelectorAll('a[href]')) {
        const u = toUrl(a.getAttribute('href'), base);
        if (!u) continue;
        const path = u.pathname.replace(/\/$/, '');
        if (path.startsWith(prefix)) {
          links.push({ a, path, url: u.origin + path });
          for (let e = a.parentElement; e; e = e.parentElement) {
            const cur = owner.get(e);
            if (cur === undefined) owner.set(e, path);
            else if (cur === path || cur === null) break;
            else owner.set(e, null);
          }
        } else if (path === listPath ? /[?&](p|page)=\d+/.test(u.search) : path.startsWith(listPath + '/page/')) {
          const next = path + u.search;           // pagination, if the list is ever split into pages
          if (!seen.has(next)) { seen.add(next); pages.push(next); }
        }
      }

      for (const { a, path, url } of links) {
        const chain = [a];
        for (let e = a.parentElement; chain.length < 4 && e && owner.get(e) === path && !/^(BODY|HTML|MAIN)$/.test(e.tagName); e = e.parentElement) chain.push(e);
        let f = null, col = null;
        for (const el of chain) {
          if (!f || !f.name) { const g = parseText(textTokens(el)); if (g && (!f || g.name)) f = g; }
          const c = findColour(el);
          if (c && (!col || c.score > col.score)) col = c;
          if (f && f.name && col && col.score >= 3) break;
        }
        let slugCode = '';
        try { slugCode = decodeURIComponent(path.slice(prefix.length)).toUpperCase(); } catch (e) { slugCode = path.slice(prefix.length); }
        const row = {
          make: mk.name, code: f ? f.code : slugCode, name: f ? f.name : clean(a.textContent),
          finish: f ? f.finish : '', hex: col ? col.hex : '', swatch_css: col ? col.css : '', url,
        };
        const key = `${row.code}|${row.name}|${row.finish}|${row.hex}`;
        if (keys.has(key)) continue;
        keys.add(key);
        rows.push(row);
        if (!sample) sample = chain[chain.length - 1].outerHTML;
      }
    }
    return { rows, expected, sample, excerpt };
  }

  // ---------- makes listed on the index page ----------
  const onIndex = location.pathname.replace(/\/$/, '') === INDEX_PATH;
  const index = onIndex ? document : await getDoc(INDEX_PATH);
  if (!index) { console.error(`Could not load ${INDEX_PATH}`); return; }
  const makes = new Map();
  for (const a of index.querySelectorAll('a[href]')) {
    const u = toUrl(a.getAttribute('href'), location.origin + INDEX_PATH);
    const m = u && u.pathname.match(/^\/trouver-code-couleur(?:-moto|-camion)?\/([^/]+)\/?$/);
    if (!m || makes.has(m[1])) continue;
    const img = a.querySelector('img');
    const name = clean(a.textContent) || clean(img ? img.alt : '').replace(/^Peinture\s+/i, '') || m[1];
    makes.set(m[1], { slug: m[1], name, path: u.pathname.replace(/\/$/, '') });
  }
  for (const slug of makes.keys()) if (!S.order.includes(slug)) S.order.push(slug);
  const todo = [...makes.values()].filter(mk => !S.done.has(mk.slug) && (!ONLY.length || ONLY.includes(mk.slug)));
  const total = todo.length;
  console.log(`${makes.size} makes found, ${total} to fetch.`);

  // ---------- run ----------
  let finished = 0, streak = 0;
  const failed = [], mismatched = [];
  const worker = async () => {
    while (todo.length) {
      const mk = todo.shift();
      const res = await scrapeMake(mk);
      finished++;
      if (!res) {
        failed.push(mk.name);
        console.warn(`[${finished}/${total}] ${mk.name}: failed (paste the script again later to retry it)`);
        if (++streak >= 5) { console.error('5 makes failed in a row, stopping. Wait a few minutes, then paste the script again to resume.'); todo.length = 0; }
      } else {
        streak = 0;
        S.byMake[mk.slug] = res.rows;
        S.done.add(mk.slug);
        const withHex = res.rows.filter(r => r.hex).length;
        const off = res.expected != null && Math.abs(res.expected - res.rows.length) > Math.max(2, res.expected * 0.02);
        if (off) mismatched.push(`${mk.name} (${res.rows.length} found, page says ${res.expected})`);
        console.log(`[${finished}/${total}] ${mk.name}: ${res.rows.length} codes, ${withHex} with colour${off ? `  ⚠ page says ${res.expected}` : ''}`);
        if (res.rows.length && !S.warned) {
          S.warned = true;
          if (!withHex) console.warn(`No swatch colour found for ${mk.name}. Names and codes are fine; for the hex, copy this card's HTML into the chat so the script can be adjusted:\n\n${res.sample.slice(0, 3000)}`);
        }
        if (!res.rows.length && res.expected && !S.warnedEmpty) {
          S.warnedEmpty = true;
          console.warn(`${mk.name}: the page lists ${res.expected} codes but none were recognised. Copy this HTML excerpt into the chat so the script can be adjusted:\n\n${res.excerpt}`);
        }
      }
      if (finished >= 3 && !Object.values(S.byMake).some(r => r.length)) {
        console.error('No codes recognised on the first pages, stopping (see the excerpt above).');
        todo.length = 0;
      }
      await sleep(DELAY_MS);
    }
  };
  await Promise.all(Array.from({ length: CONCURRENCY }, worker));

  const all = S.order.flatMap(slug => S.byMake[slug] || []);
  console.log(`Done in ${Math.round((Date.now() - t0) / 1000)} s: ${all.length} codes from ${S.done.size} makes, ${all.filter(r => r.hex).length} with a colour.`);
  if (mismatched.length) console.warn('Counts differ from the page for: ' + mismatched.join('; '));
  if (failed.length) console.warn('Failed: ' + failed.join(', ') + '. Paste the script again to retry just these.');
  if (all.length) { console.table(all.slice(0, 10)); console.log(window.bspDownload()); }
})();
