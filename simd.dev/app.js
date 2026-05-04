/* simd.dev landing page — search box + result card.
 *
 * Loads simd-data.json once on init (it's the source of truth for everything
 * the site shows). All names, type-set, ambiguous-alias map, and per-name
 * arch/family info come from there -- no separate names index needed.
 *
 * Search syntax:
 *   plain tokens          → substring match against the name (case-insensitive)
 *   arch:VALUE            → keep only names whose record.arch list contains VALUE
 *                           (substring-match on each entry, case-insensitive)
 *   family:VALUE          → same as arch but matched against record.family
 *
 * Multiple terms compose with AND. Filter chips below the search box toggle
 * the corresponding `arch:`/`family:` token in the input.
 */

(function () {
    'use strict';

    const DATA_URL    = 'dist/simd-data.json';
    // We render up to this many result chips. Past this, the user is told
    // the count and asked to refine.
    const MAX_RESULTS = 1000;

    const $q              = document.getElementById('q');
    const $status         = document.getElementById('search-status');
    const $results        = document.getElementById('results');
    const $resultsSection = document.getElementById('results-section');
    const $resultsCount   = document.getElementById('results-count');
    const $card           = document.getElementById('card');
    const $archChips      = document.getElementById('arch-chips');
    const $famChips       = document.getElementById('family-chips');
    const $famToggle      = document.getElementById('family-toggle');

    let allNames = null;       // sorted array of all callable names + ambiguous aliases
    let typeSet  = null;       // Set<string> of names where kind === 'type'
    let records  = null;       // {name: rec}
    let ambiguous = null;      // {alias: [canonical, ...]}
    let clusters = null;       // {cluster_id: [name, ...]} -- variant groups

    init().catch(err => {
        $status.textContent = 'failed to load database';
        console.error('simd.dev load failure:', err);
    });

    async function init() {
        $status.textContent = 'loading…';
        const doc = await fetchJSON(DATA_URL);
        records   = doc.records   || {};
        ambiguous = doc.ambiguous || {};
        clusters  = doc.clusters  || {};

        // Names = canonicals (records keys) ∪ ambiguous aliases.
        const set = new Set(Object.keys(records));
        for (const a of Object.keys(ambiguous)) set.add(a);
        allNames = [...set].sort();
        typeSet = new Set();
        for (const [n, r] of Object.entries(records)) if (r.kind === 'type') typeSet.add(n);

        $status.textContent = allNames.length.toLocaleString() + ' indexed';

        renderChips();

        $q.addEventListener('input', onInput);
        $q.addEventListener('keydown', onKeydown);
        $results.addEventListener('click', onResultsClick);
        // Card click delegate: tab headers (open one body, close others),
        // variant chips (load that sibling's card), and family/arch tag
        // chips (toggle the corresponding filter).
        $card.addEventListener('click', (ev) => {
            const modeBtn = ev.target.closest('button.ex-mode[data-mode]');
            if (modeBtn) {
                const ex = modeBtn.closest('.ex');
                if (ex) {
                    const isHex = ex.classList.toggle('hex');
                    for (const b of ex.querySelectorAll('button.ex-mode')) {
                        const active = (b.dataset.mode === 'hex') === isHex;
                        b.classList.toggle('is-active', active);
                        b.setAttribute('aria-pressed', active ? 'true' : 'false');
                    }
                }
                return;
            }
            const tab = ev.target.closest('button.card-tab[data-tab]');
            if (tab) {
                const id = tab.dataset.tab;
                const wasActive = tab.getAttribute('aria-expanded') === 'true';
                for (const t of $card.querySelectorAll('button.card-tab')) {
                    t.setAttribute('aria-expanded', 'false');
                }
                for (const b of $card.querySelectorAll('.card-tab-body')) {
                    b.hidden = true;
                }
                if (!wasActive) {
                    tab.setAttribute('aria-expanded', 'true');
                    const body = $card.querySelector('.card-tab-body[data-body="' + id + '"]');
                    if (body) body.hidden = false;
                }
                return;
            }
            const variant = ev.target.closest('button.variant[data-name]');
            if (variant) { showCard(variant.dataset.name); return; }
            const filterTag = ev.target.closest('button.tag-filter[data-field]');
            if (filterTag) toggleFilter(filterTag.dataset.field, filterTag.dataset.value);
        });

        const $home = document.getElementById('home-link');
        if ($home) $home.addEventListener('click', (ev) => {
            // Reset to a clean URL + cleared search state, without a full reload.
            // Falls back to the href attribute if JS fails for any reason.
            ev.preventDefault();
            $q.value = '';
            $card.hidden = true;
            renderResults('');
            refreshChipState();
            history.pushState(null, '', location.pathname + location.search);
            window.scrollTo({ top: 0, behavior: 'smooth' });
            $q.focus();
        });
        document.querySelectorAll('code[data-q]').forEach(c => {
            c.style.cursor = 'pointer';
            c.addEventListener('click', () => {
                $q.value = c.dataset.q;
                $q.focus();
                renderResults($q.value);
                refreshChipState();
            });
        });

        // If the URL has #intrinsic, jump to it.
        if (location.hash.length > 1) {
            const target = decodeURIComponent(location.hash.slice(1));
            if (records[target] || ambiguous[target]) {
                $q.value = target;
                renderResults(target);
                showCard(target);
            }
        }

        $q.focus();
    }

    function onInput() {
        // Any keystroke in the search box should hide the currently-open card --
        // the user is starting a new search, the previous result is stale.
        $card.hidden = true;
        renderResults($q.value);
        refreshChipState();
    }

    function onKeydown(e) {
        if (e.key === 'Enter') {
            const first = $results.querySelector('button');
            if (first) { first.click(); e.preventDefault(); }
        } else if (e.key === 'Escape') {
            $q.value = '';
            renderResults('');
            refreshChipState();
            $card.hidden = true;
        }
    }

    // -------------------------------------------------------------------
    // Query parsing + filtering
    // -------------------------------------------------------------------
    function parseQuery(s) {
        const filters = [];
        const terms = [];
        const tokens = (s || '').match(/\S+/g) || [];
        for (const t of tokens) {
            const m = t.match(/^(arch|family):(.+)$/i);
            if (m) {
                filters.push({ key: m[1].toLowerCase(), value: m[2].toLowerCase() });
            } else {
                terms.push(t.toLowerCase());
            }
        }
        return { filters, terms };
    }

    function unparseQuery(q) {
        const parts = [];
        for (const f of q.filters) parts.push(f.key + ':' + f.value);
        for (const t of q.terms) parts.push(t);
        return parts.join(' ');
    }

    function recordMetaFor(name) {
        // Returns { archs, families } for a name. For ambiguous aliases, union
        // the targets' arch/family.
        const r = records[name];
        if (r) return { archs: r.arch || [], families: r.family || [] };
        const targets = ambiguous[name];
        if (!targets) return { archs: [], families: [] };
        const aset = new Set(), fset = new Set();
        for (const t of targets) {
            const tr = records[t];
            if (!tr) continue;
            for (const a of tr.arch || []) aset.add(a);
            for (const f of tr.family || []) fset.add(f);
        }
        return { archs: [...aset], families: [...fset] };
    }

    function passesFilters(name, filters) {
        if (filters.length === 0) return true;
        const meta = recordMetaFor(name);
        for (const f of filters) {
            const list = f.key === 'arch' ? meta.archs : meta.families;
            const v = f.value;
            if (!list.some(x => x.toLowerCase().indexOf(v) >= 0)) return false;
        }
        return true;
    }

    // -------------------------------------------------------------------
    // Results
    // -------------------------------------------------------------------
    function renderResults(query) {
        $results.innerHTML = '';
        $resultsCount.textContent = '';
        if (!records) { $resultsSection.hidden = true; return; }
        const q = parseQuery(query || '');

        // No terms and no filters → hide the section entirely (don't dump
        // the whole catalog).
        if (q.terms.length === 0 && q.filters.length === 0) {
            $resultsSection.hidden = true;
            return;
        }

        // Collect *all* matches across the whole catalog (no early break) so
        // the total count is accurate. Render up to MAX_RESULTS.
        //
        // Multi-word terms compose with AND on substring match, so e.g.
        // "add f16" matches vpadd_f16 (both 'add' and 'f16' appear in the
        // name) but not vadd_s8 (no 'f16').
        const exact = [], prefix = [], sub = [];
        const terms = q.terms;
        for (const n of allNames) {
            if (!passesFilters(n, q.filters)) continue;

            const ln = n.toLowerCase();
            if (terms.length === 0) { sub.push(n); continue; }

            let allMatch = true;
            for (const t of terms) {
                if (ln.indexOf(t) < 0) { allMatch = false; break; }
            }
            if (!allMatch) continue;

            // Rank: exact name match (single-term), prefix on any term, else sub.
            if (terms.length === 1 && ln === terms[0]) exact.push(n);
            else if (terms.some(t => ln.startsWith(t))) prefix.push(n);
            else sub.push(n);
        }
        const ranked = [...exact, ...prefix, ...sub];
        const total = ranked.length;
        const visible = ranked.slice(0, MAX_RESULTS);

        $resultsSection.hidden = false;

        if (total === 0) {
            $resultsCount.textContent = 'no matches';
            const li = document.createElement('li');
            li.innerHTML = '<span class="muted" style="padding:3px 6px;">no matches</span>';
            $results.appendChild(li);
            return;
        }

        $resultsCount.textContent = total > MAX_RESULTS
            ? `showing ${MAX_RESULTS.toLocaleString()} of ${total.toLocaleString()} — refine to narrow down`
            : `${total.toLocaleString()} match${total === 1 ? '' : 'es'}`;

        const frag = document.createDocumentFragment();
        for (const n of visible) {
            const li = document.createElement('li');
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.textContent = n;
            btn.dataset.name = n;
            if (typeSet.has(n)) btn.classList.add('is-type');
            li.appendChild(btn);
            frag.appendChild(li);
        }
        $results.appendChild(frag);
    }

    function onResultsClick(ev) {
        const btn = ev.target.closest('button[data-name]');
        if (!btn) return;
        showCard(btn.dataset.name);
    }

    function showCard(name) {
        const rec = records[name];
        const ambig = ambiguous[name];
        if (!rec && ambig) {
            const list = ambig.slice(0, 8).map(n => `<code>${escapeHtml(n)}</code>`).join(', ');
            const more = ambig.length > 8 ? ` <em>+${ambig.length - 8} more</em>` : '';
            $card.innerHTML = `<div class="card-head"><span class="name">${escapeHtml(name)}</span> <span class="tag">overloaded</span></div>
                <div>Resolves to ${ambig.length} typed variants: ${list}${more}</div>`;
            $card.hidden = false;
            $card.classList.remove('is-type');
            history.replaceState(null, '', '#' + encodeURIComponent(name));
            $card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            return;
        }
        if (!rec) {
            $card.innerHTML = `<div class="muted"><code>${escapeHtml(name)}</code> not in database.</div>`;
            $card.hidden = false;
            return;
        }
        $card.innerHTML = renderCardHTML(name, rec);
        $card.hidden = false;
        $card.classList.toggle('is-type', rec.kind === 'type');
        history.replaceState(null, '', '#' + encodeURIComponent(name));
        $card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    function renderCardHTML(name, rec) {
        // Tags: family + arch tags are clickable buttons that toggle the
        // corresponding filter chip. The "type" badge stays static.
        const tags = [];
        if (rec.kind === 'type') tags.push(`<span class="tag kind">type</span>`);
        for (const f of rec.family || []) {
            const filterValue = (f === 'SME and SME2' ? 'SME' : f).toLowerCase();
            tags.push(`<button type="button" class="tag tag-filter" data-field="family" data-value="${escapeAttr(filterValue)}" title="filter family:${escapeAttr(filterValue)}">${escapeHtml(f)}</button>`);
        }
        for (const a of rec.arch || []) {
            tags.push(`<button type="button" class="tag tag-filter arch" data-field="arch" data-value="${escapeAttr(a.toLowerCase())}" title="filter arch:${escapeAttr(a)}">${escapeHtml(a)}</button>`);
        }

        // Tab row: pseudocode + variants share one row of header buttons,
        // mutually exclusive content underneath. Click handler on the card.
        const tabs = [];
        if (rec.pseudocode) {
            tabs.push({ id: 'pc', label: 'pseudocode',
                body: `<pre class="card-pc-body">${escapeHtml(rec.pseudocode)}</pre>` });
        }
        if (rec.example) {
            tabs.push({ id: 'ex', label: 'example',
                body: renderExample(rec.example) });
        }
        if (rec.cluster && clusters && clusters[rec.cluster]) {
            const siblings = clusters[rec.cluster].filter(n => n !== name);
            if (siblings.length > 0) {
                const chips = siblings.map(n => `<button type="button" class="variant" data-name="${escapeAttr(n)}">${escapeHtml(n)}</button>`).join('');
                tabs.push({ id: 'vars', label: siblings.length + ' variants',
                    body: `<div class="variants-list">${chips}</div>` });
            }
        }
        let togglesRow = '';
        if (tabs.length) {
            const headers = tabs.map(t =>
                `<button type="button" class="card-tab" data-tab="${t.id}" aria-expanded="false">${t.label}</button>`
            ).join('');
            const bodies = tabs.map(t =>
                `<div class="card-tab-body" data-body="${t.id}" hidden>${t.body}</div>`
            ).join('');
            togglesRow = `<div class="card-toggles">${headers}${bodies}</div>`;
        }

        const linkLabel = rec.source === 'arm-acle' ? 'Arm developer docs →' : 'Intel Intrinsics Guide →';
        const links = [];
        if (rec.doc_url) links.push(`<a class="upstream-link" href="${escapeAttr(rec.doc_url)}" target="_blank" rel="noopener">${linkLabel}</a>`);
        const ceUrl = (window.SimdTooltips && window.SimdTooltips.compilerExplorerUrl)
            ? window.SimdTooltips.compilerExplorerUrl(rec)
            : null;
        if (ceUrl) links.push(`<a class="upstream-link" href="${escapeAttr(ceUrl)}" target="_blank" rel="noopener">Compiler Explorer →</a>`);
        const link = links.length ? `<div class="upstream-links">${links.join(' &middot; ')}</div>` : '';

        const desc = rec.description ? `<div class="description">${escapeHtml(rec.description)}</div>` : '';

        return `
            <div class="card-head">
                <span class="name">${escapeHtml(name)}</span>
                ${tags.join(' ')}
            </div>
            <pre class="signature">${escapeHtml(rec.definition || '')}</pre>
            ${desc}
            ${togglesRow}
            ${link}
        `;
    }

    // -------------------------------------------------------------------
    // Filter chips
    // -------------------------------------------------------------------
    function renderChips() {
        const archCount = new Map();
        const famCount = new Map();
        for (const [n, r] of Object.entries(records)) {
            for (const a of r.arch   || []) archCount.set(a, (archCount.get(a) || 0) + 1);
            for (const f of r.family || []) famCount.set(f, (famCount.get(f) || 0) + 1);
        }
        const fmt = n => n.toLocaleString();
        const sortByCount = m => [...m.entries()].sort((a, b) => b[1] - a[1]);

        const archEntries = sortByCount(archCount);
        const famEntries  = sortByCount(famCount);

        $archChips.innerHTML = archEntries.map(([name, count]) => chipHtml('arch', name, count)).join('');
        $famChips.innerHTML  = famEntries.map(([name, count])  => chipHtml('family', name, count, name === 'SME and SME2' ? 'SME' : null)).join('');

        $archChips.addEventListener('click', onChipClick);
        $famChips.addEventListener('click', onChipClick);

        // Family chip row is collapsed by default (~80 families). The toggle
        // expands the row to show all of them.
        if ($famToggle) {
            $famToggle.addEventListener('click', () => {
                const expanded = $famChips.classList.toggle('expanded');
                $famToggle.textContent = expanded ? 'less…' : 'more…';
                $famToggle.setAttribute('aria-expanded', expanded ? 'true' : 'false');
            });
        }
    }

    function chipHtml(field, label, count, filterValueOverride) {
        const value = filterValueOverride || label;
        return `<button type="button" class="chip" data-field="${field}" data-value="${escapeAttr(value.toLowerCase())}" title="${escapeAttr(field + ':' + value)}">${escapeHtml(label)}<span class="chip-count">${count.toLocaleString()}</span></button>`;
    }

    function onChipClick(ev) {
        const btn = ev.target.closest('button.chip');
        if (!btn) return;
        const field = btn.dataset.field;
        const value = btn.dataset.value;
        toggleFilter(field, value);
    }

    function toggleFilter(field, value) {
        // Chip clicks aren't composable -- they reset the query to just this
        // single filter (or clear it entirely when toggling off the active chip).
        // Any other content the user had typed gets wiped.
        const q = parseQuery($q.value);
        const isActive = q.filters.some(f => f.key === field && f.value === value);
        $q.value = isActive ? '' : (field + ':' + value);
        $card.hidden = true;
        renderResults($q.value);
        refreshChipState();
        $q.focus();
    }

    function refreshChipState() {
        const q = parseQuery($q.value);
        const active = new Set(q.filters.map(f => f.key + ':' + f.value));
        for (const btn of document.querySelectorAll('button.chip')) {
            const key = btn.dataset.field + ':' + btn.dataset.value;
            btn.classList.toggle('active', active.has(key));
        }
    }

    // -------------------------------------------------------------------
    // helpers
    // -------------------------------------------------------------------
    async function fetchJSON(url) {
        const r = await fetch(url, { credentials: 'omit' });
        if (!r.ok) throw new Error(`HTTP ${r.status} for ${url}`);
        return r.json();
    }
    function escapeHtml(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
            ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }
    function escapeAttr(s) { return escapeHtml(s); }
    function renderExample(ex) {
        const inputs = ex.inputs || [];
        const out = ex.output || {};
        const outVals = Array.isArray(out.values) ? out.values : [out.values];

        function laneInfo(typeName) {
            if (!typeName) return { bits: null, kind: null };
            if (/^const\s+(?:unsigned\s+)?int$/.test(typeName)) return { bits: 32, kind: 'int' };
            let m = typeName.match(/^(u?)int(8|16|32|64|128)(?:x\d+)?_t$/);
            if (m) return { bits: +m[2], kind: m[1] ? 'uint' : 'int' };
            m = typeName.match(/^poly(8|16|32|64|128)(?:x\d+)?_t$/);
            if (m) return { bits: +m[1], kind: 'poly' };
            m = typeName.match(/^bfloat(16)(?:x\d+)?_t$/);
            if (m) return { bits: 16, kind: 'bfloat' };
            m = typeName.match(/^(?:m?float)(8|16|32|64)(?:x\d+)?_t$/);
            if (m) return { bits: +m[1], kind: 'float' };
            return { bits: null, kind: null };
        }
        function hexFromValue(v, bits, kind) {
            if (bits == null) return String(v);
            const len = bits / 4;
            if (kind === 'float' || kind === 'bfloat') {
                const f = Number(v);
                if (kind === 'bfloat') {
                    const buf = new ArrayBuffer(4);
                    new Float32Array(buf)[0] = f;
                    const u = new Uint32Array(buf)[0];
                    return ((u >>> 16) & 0xffff).toString(16).padStart(4, '0');
                }
                if (bits === 32) {
                    const buf = new ArrayBuffer(4);
                    new Float32Array(buf)[0] = f;
                    return new Uint32Array(buf)[0].toString(16).padStart(8, '0');
                }
                if (bits === 64) {
                    const buf = new ArrayBuffer(8);
                    new Float64Array(buf)[0] = f;
                    return new BigUint64Array(buf)[0].toString(16).padStart(16, '0');
                }
                if (bits === 16 && typeof Float16Array !== 'undefined') {
                    const buf = new ArrayBuffer(2);
                    new Float16Array(buf)[0] = f;
                    return new Uint16Array(buf)[0].toString(16).padStart(4, '0');
                }
                return String(v);
            }
            if (bits <= 32) {
                const mask = bits === 32 ? 0xffffffff : ((1 << bits) - 1);
                const u = (Number(v) & mask) >>> 0;
                return u.toString(16).padStart(len, '0');
            }
            let n = typeof v === 'bigint' ? v : BigInt(v);
            if (n < 0n) n = (1n << 64n) + n;
            return n.toString(16).padStart(len, '0');
        }
        function hexFromBytes(bytesHex, laneIdx, bits) {
            const lb = bits / 8;
            const slice = bytesHex.slice(laneIdx * lb * 2, (laneIdx + 1) * lb * 2);
            return slice.match(/.{1,2}/g).reverse().join('');
        }

        const rows = [];
        for (const inp of inputs) {
            const { bits, kind } = laneInfo(inp.type);
            const vals = Array.isArray(inp.values) ? inp.values : [inp.values];
            rows.push({
                label: (inp.name || '') + ':',
                values: vals,
                hexes: vals.map(v => hexFromValue(v, bits, kind)),
                cls: '',
            });
        }
        {
            const { bits, kind } = laneInfo(out.type);
            const hexes = outVals.map((v, i) =>
                out.bytes_hex && bits ? hexFromBytes(out.bytes_hex, i, bits) : hexFromValue(v, bits, kind)
            );
            rows.push({ label: '→', values: outVals, hexes, cls: 'ex-out', isOut: true });
        }

        const lanes = Math.max(1, ...rows.map(r => r.values.length));
        const cells = [];
        for (const row of rows) {
            const labelClass = row.isOut ? 'ex-lbl ex-arrow' : 'ex-lbl';
            cells.push(`<span class="${labelClass}">${escapeHtml(row.label)}</span>`);
            for (let i = 0; i < lanes; i++) {
                if (i < row.values.length) {
                    const dec = String(row.values[i]);
                    const hex = row.hexes[i] || '';
                    cells.push(
                        `<span class="ex-val ${row.cls}">` +
                        `<span class="ex-dec">${escapeHtml(dec)}</span>` +
                        `<span class="ex-hexcell">${escapeHtml(hex)}</span>` +
                        `</span>`
                    );
                } else {
                    cells.push(`<span class="ex-val"></span>`);
                }
            }
        }
        const cols = `auto repeat(${lanes}, max-content)`;
        const toggle =
            `<div class="ex-modes" role="group" aria-label="number base">` +
            `<button type="button" class="ex-mode is-active" data-mode="dec" aria-pressed="true">dec</button>` +
            `<button type="button" class="ex-mode" data-mode="hex" aria-pressed="false">hex</button>` +
            `</div>`;
        return `<div class="ex" style="grid-template-columns:${cols}">${toggle}${cells.join('')}</div>`;
    }
})();
