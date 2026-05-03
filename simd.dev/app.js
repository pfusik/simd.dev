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

    init().catch(err => {
        $status.textContent = 'failed to load database';
        console.error('simd.dev load failure:', err);
    });

    async function init() {
        $status.textContent = 'loading…';
        const doc = await fetchJSON(DATA_URL);
        records   = doc.records || {};
        ambiguous = doc.ambiguous || {};

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
        const tags = [];
        if (rec.kind === 'type') tags.push(`<span class="tag kind">type</span>`);
        for (const f of rec.family || []) tags.push(`<span class="tag">${escapeHtml(f)}</span>`);
        for (const a of rec.arch   || []) tags.push(`<span class="tag arch">${escapeHtml(a)}</span>`);

        let pseudoblock = '';
        if (rec.pseudocode) {
            pseudoblock = `<details><summary class="pseudocode-toggle"> pseudocode</summary><pre>${escapeHtml(rec.pseudocode)}</pre></details>`;
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
            ${pseudoblock}
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
})();
