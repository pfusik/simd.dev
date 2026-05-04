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
    const PAGE_TITLE = document.title;

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
        // Select the entire value when an editable cell gets focus
        // (click or tab) so the user can immediately overwrite. Defer
        // with rAF so we run *after* the browser places the caret.
        $card.addEventListener('focusin', (ev) => {
            const cell = ev.target.closest('.ex-dec[contenteditable]');
            if (!cell) return;
            requestAnimationFrame(() => {
                if (document.activeElement !== cell) return;
                const range = document.createRange();
                range.selectNodeContents(cell);
                const sel = window.getSelection();
                sel.removeAllRanges();
                sel.addRange(range);
            });
        });

        // When a user edits an input cell, mark the output as stale
        // (grayed out) until a fresh "run on CE" finishes.
        $card.addEventListener('input', (ev) => {
            const cell = ev.target.closest('.ex-dec[contenteditable]');
            if (!cell) return;
            const wrap = cell.closest('.ex-wrap');
            if (wrap) wrap.classList.add('is-stale');
            const status = wrap && wrap.querySelector('.ex-status');
            if (status) {
                status.textContent = 'edited — click run to recompile';
                status.className = 'ex-status is-stale';
            }
        });

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
            const runBtn = ev.target.closest('button.ex-run');
            if (runBtn) {
                ev.preventDefault();
                runOnCE($card.querySelector('.ex'));
                return;
            }
            const seeBtn = ev.target.closest('button.ex-see');
            if (seeBtn) {
                ev.preventDefault();
                seeOnCE($card.querySelector('.ex'));
                return;
            }
            const more = ev.target.closest('button.variants-more');
            if (more) {
                // Reveal the hidden tail of variants and drop the button.
                const tail = more.previousElementSibling;
                if (tail && tail.classList.contains('variants-tail')) tail.hidden = false;
                more.remove();
                return;
            }
            const variant = ev.target.closest('button.variant[data-name]');
            if (variant) {
                // On the dedicated page, navigate to that variant's page;
                // on the compact card, just swap to the variant's card.
                if (document.body.classList.contains('intrinsic-mode')) {
                    const target = variant.dataset.name;
                    history.pushState(null, '', '?intrinsic=' + encodeURIComponent(target));
                    enterIntrinsicPage(target);
                } else {
                    showCard(variant.dataset.name);
                }
                return;
            }
            const filterTag = ev.target.closest('button.tag-filter[data-field]');
            if (filterTag) {
                if (document.body.classList.contains('intrinsic-mode')) {
                    // On the dedicated page, a filter chip means "go to
                    // search filtered by this". Drop the intrinsic param.
                    history.pushState(null, '', './');
                    exitIntrinsicPage({ updateHistory: false });
                }
                toggleFilter(filterTag.dataset.field, filterTag.dataset.value);
            }
            const back = ev.target.closest('a[data-back="1"]');
            if (back) {
                ev.preventDefault();
                exitIntrinsicPage();
                return;
            }
            // Compact-card name → dedicated page (in-place, no reload).
            const nameLink = ev.target.closest('a.name-link');
            if (nameLink) {
                ev.preventDefault();
                const u = new URL(nameLink.href, location.href);
                const target = u.searchParams.get('intrinsic');
                if (target) {
                    history.pushState(null, '', '?intrinsic=' + encodeURIComponent(target));
                    enterIntrinsicPage(target);
                }
                return;
            }
            // Ambiguous-alias variant link inside the dedicated page.
            const ambigLink = ev.target.closest('a[href^="?intrinsic="]');
            if (ambigLink) {
                ev.preventDefault();
                const u = new URL(ambigLink.href, location.href);
                const target = u.searchParams.get('intrinsic');
                if (target) {
                    history.pushState(null, '', '?intrinsic=' + encodeURIComponent(target));
                    enterIntrinsicPage(target);
                }
                return;
            }
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

        // ?intrinsic=NAME -- dedicated full-page view. Takes priority over
        // the legacy #hash form. Reload-friendly, shareable.
        const params = new URLSearchParams(location.search);
        const intrinsicParam = params.get('intrinsic');
        if (intrinsicParam) {
            enterIntrinsicPage(intrinsicParam);
            return;
        }

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

    // Browser back/forward: re-evaluate URL and toggle modes as needed.
    window.addEventListener('popstate', () => {
        const params = new URLSearchParams(location.search);
        const target = params.get('intrinsic');
        if (target) {
            enterIntrinsicPage(target);
        } else if (document.body.classList.contains('intrinsic-mode')) {
            exitIntrinsicPage({ updateHistory: false });
        }
    });

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

    // opts:
    //   expanded -- if true, render every section open and labeled
    //               (no tab buttons); used by the dedicated /?intrinsic
    //               page. If false (default), render the compact card
    //               with the tabbed UI.
    function renderCardHTML(name, rec, opts = {}) {
        const expanded = !!opts.expanded;

        const tags = [];
        if (rec.kind === 'type') tags.push(`<span class="tag kind">type</span>`);
        for (const f of rec.family || []) {
            const filterValue = (f === 'SME and SME2' ? 'SME' : f).toLowerCase();
            tags.push(`<button type="button" class="tag tag-filter" data-field="family" data-value="${escapeAttr(filterValue)}" title="filter family:${escapeAttr(filterValue)}">${escapeHtml(f)}</button>`);
        }
        for (const a of rec.arch || []) {
            tags.push(`<button type="button" class="tag tag-filter arch" data-field="arch" data-value="${escapeAttr(a.toLowerCase())}" title="filter arch:${escapeAttr(a)}">${escapeHtml(a)}</button>`);
        }

        // Sections: pseudocode + example + variants. In compact mode they
        // share a tab row (mutually exclusive). In expanded mode each is
        // a labeled, always-visible section.
        const sections = [];
        if (rec.pseudocode) {
            sections.push({ id: 'pc', label: 'pseudocode',
                body: `<pre class="card-pc-body">${escapeHtml(rec.pseudocode)}</pre>` });
        }
        if (rec.example) {
            sections.push({ id: 'ex', label: 'example',
                body: renderExample(rec.example, name) });
        }
        if (rec.cluster && clusters && clusters[rec.cluster]) {
            const siblings = clusters[rec.cluster].filter(n => n !== name);
            if (siblings.length > 0) {
                const VAR_LIMIT = 50;
                const head = siblings.slice(0, VAR_LIMIT);
                const tail = siblings.slice(VAR_LIMIT);
                const chip = n => `<button type="button" class="variant" data-name="${escapeAttr(n)}">${escapeHtml(n)}</button>`;
                let html = head.map(chip).join('');
                if (tail.length) {
                    html += `<span class="variants-tail" hidden>${tail.map(chip).join('')}</span>`;
                    html += `<button type="button" class="variants-more" data-more="1">+${tail.length} more</button>`;
                }
                sections.push({ id: 'vars', label: siblings.length + ' variants',
                    body: `<div class="variants-list">${html}</div>` });
            }
        }

        let body = '';
        if (sections.length) {
            if (expanded) {
                // Dedicated-page mode: each section is a <details open> so
                // the user can collapse what they don't want without
                // losing the others. All open by default.
                body = sections.map(s =>
                    `<details class="card-section" data-section="${s.id}" open>
                        <summary class="card-section-title">${s.label}</summary>
                        ${s.body}
                    </details>`
                ).join('');
            } else {
                const headers = sections.map(s =>
                    `<button type="button" class="card-tab" data-tab="${s.id}" aria-expanded="false">${s.label}</button>`
                ).join('');
                const bodies = sections.map(s =>
                    `<div class="card-tab-body" data-body="${s.id}" hidden>${s.body}</div>`
                ).join('');
                body = `<div class="card-toggles">${headers}${bodies}</div>`;
            }
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
        // Compact card: name links to the dedicated /?intrinsic page.
        // Expanded page: name is plain text (we're already on its page).
        const nameNode = expanded
            ? `<span class="name">${escapeHtml(name)}</span>`
            : `<a class="name name-link" href="?intrinsic=${encodeURIComponent(name)}">${escapeHtml(name)}</a>`;
        // Top-right "open dedicated page" link on the compact card.
        const openLink = expanded
            ? ''
            : `<a class="card-open name-link" href="?intrinsic=${encodeURIComponent(name)}" title="Open dedicated page">↗</a>`;

        return `
            <div class="card-head">
                ${nameNode}
                ${tags.join(' ')}
                ${openLink}
            </div>
            <pre class="signature">${escapeHtml(rec.definition || '')}</pre>
            ${desc}
            ${body}
            ${link}
        `;
    }

    function renderIntrinsicPageHTML(name, rec) {
        const back = `<nav class="page-nav"><a href="./" data-back="1">← back to search</a></nav>`;
        _renderExampleViewable = isLiveViewable(rec);
        _renderExampleRunnable = isLiveRunnable(rec);
        try {
            return back + renderCardHTML(name, rec, { expanded: true });
        } finally {
            _renderExampleViewable = false;
            _renderExampleRunnable = false;
        }
    }

    function enterIntrinsicPage(name) {
        const rec = records[name];
        const ambig = ambiguous[name];
        document.body.classList.add('intrinsic-mode');
        $card.classList.add('card-page');
        $card.hidden = false;
        if (!rec && ambig) {
            const list = ambig.slice(0, 8).map(n => `<a href="?intrinsic=${encodeURIComponent(n)}"><code>${escapeHtml(n)}</code></a>`).join(', ');
            const more = ambig.length > 8 ? ` <em>+${ambig.length - 8} more</em>` : '';
            $card.innerHTML = `<nav class="page-nav"><a href="./" data-back="1">← back to search</a></nav>
                <div class="card-head"><span class="name">${escapeHtml(name)}</span> <span class="tag">overloaded</span></div>
                <div>Resolves to ${ambig.length} typed variants: ${list}${more}</div>`;
        } else if (!rec) {
            $card.innerHTML = `<nav class="page-nav"><a href="./" data-back="1">← back to search</a></nav>
                <div class="muted"><code>${escapeHtml(name)}</code> not in database.</div>`;
        } else {
            $card.innerHTML = renderIntrinsicPageHTML(name, rec);
            $card.classList.toggle('is-type', rec.kind === 'type');
        }
        document.title = name + ' · simd.dev';
        window.scrollTo({ top: 0 });
    }

    function exitIntrinsicPage(opts = {}) {
        document.body.classList.remove('intrinsic-mode');
        $card.classList.remove('card-page');
        $card.classList.remove('is-type');
        $card.hidden = true;
        $card.innerHTML = '';
        document.title = PAGE_TITLE;
        if (opts.updateHistory !== false) {
            history.pushState(null, '', './');
        }
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

    // -------------------------------------------------------------------
    // Type introspection (shared between renderExample and the live-edit
    // CE flow). Hoisted to module scope so both can use them.
    // -------------------------------------------------------------------
    function laneInfo(typeName) {
        if (!typeName) return { bits: null, kind: null };
        if (/^const\s+(?:unsigned\s+)?int$/.test(typeName)) return { bits: 32, kind: 'int' };
        let m = typeName.match(/^(u?)int(8|16|32|64|128)(?:x\d+)*_t$/);
        if (m) return { bits: +m[2], kind: m[1] ? 'uint' : 'int' };
        m = typeName.match(/^poly(8|16|32|64|128)(?:x\d+)*_t$/);
        if (m) return { bits: +m[1], kind: 'poly' };
        m = typeName.match(/^bfloat(16)(?:x\d+)*_t$/);
        if (m) return { bits: 16, kind: 'bfloat' };
        m = typeName.match(/^(?:m?float)(8|16|32|64)(?:x\d+)*_t$/);
        if (m) return { bits: +m[1], kind: 'float' };
        return { bits: null, kind: null };
    }
    function tupleCount(typeName) {
        const m = (typeName || '').match(/^[a-z]+\d+x(\d+)x[234]_t$/);
        return m ? +m[1] : 0;
    }

    // -------------------------------------------------------------------
    // Live "edit + run on Compiler Explorer" for fold-eligible intrinsics
    // on the dedicated /?intrinsic page.
    // -------------------------------------------------------------------

    // Two-level eligibility:
    //
    //   isLiveViewable -- can we generate a valid C++ harness so "see on
    //                     CE" links work? Excludes pointer params (we
    //                     don't synthesize input buffers yet).
    //   isLiveRunnable -- can we round-trip via CE's compile API and
    //                     parse RESULT bytes back? Requires the cached
    //                     entry to have come from the fold path so we
    //                     can read .rodata directly. (Execute-verified
    //                     entries can still be opened in CE for
    //                     inspection -- the user just won't get a live
    //                     re-decoded output row.)
    function isLiveViewable(rec) {
        if (!rec || !rec.example) return false;
        for (const inp of rec.example.inputs || []) {
            if (/\*/.test(inp.type)) return false;
        }
        return true;
    }
    function isLiveRunnable(rec) {
        if (!isLiveViewable(rec)) return false;
        const v = rec.example.verified_via;
        return v === 'fold' || v === 'execute';
    }

    function laneLiteral(v, info) {
        if (info.kind === 'float') {
            if (info.bits === 32) return Number(v) + 'f';
            if (info.bits === 16) return '((__fp16)' + Number(v) + 'f)';
            return String(Number(v));  // double
        }
        if (info.kind === 'bfloat') return '((__bf16)' + Number(v) + 'f)';
        return String(v);
    }

    // ----- Intel x86 helpers (mirroring scribe.py's intel paths) ----------
    const INTEL_VEC_BYTES = {
        '__m64': 8,
        '__m128': 16, '__m128d': 16, '__m128i': 16, '__m128h': 16, '__m128bh': 16,
        '__m256': 32, '__m256d': 32, '__m256i': 32, '__m256h': 32, '__m256bh': 32,
        '__m512': 64, '__m512d': 64, '__m512i': 64, '__m512h': 64, '__m512bh': 64,
    };
    const INTEL_IMM_TYPES = new Set([
        'const int', 'const unsigned int',
        'int', 'unsigned int', 'unsigned',
        '__int32', '__int8',
    ]);
    function isIntelIntrinsic(name) {
        return /^_(mm|tile)/.test(name || '');
    }
    function intelLaneInfoFor(intrinsicName, cType, context) {
        const bytesTotal = INTEL_VEC_BYTES[cType];
        if (bytesTotal == null) return null;
        const epRe = /_e?p[iu](8|16|32|64)\b/g;
        const matches = [];
        let m;
        while ((m = epRe.exec(intrinsicName))) matches.push(m);
        if (matches.length) {
            const pick = context === 'output' ? matches[matches.length - 1] : matches[0];
            const bits = +pick[1];
            const kind = /u/.test(pick[0]) ? 'uint' : 'int';
            return { bits, kind, count: (bytesTotal * 8) / bits };
        }
        if (/_si\d+\b/.test(intrinsicName)) return { bits: 8, kind: 'uint', count: bytesTotal };
        if (/_(?:ps|ss)\b/.test(intrinsicName)) return { bits: 32, kind: 'float', count: bytesTotal / 4 };
        if (/_(?:pd|sd)\b/.test(intrinsicName)) return { bits: 64, kind: 'float', count: bytesTotal / 8 };
        if (/_(?:ph|sh)\b/.test(intrinsicName)) return { bits: 16, kind: 'float', count: bytesTotal / 2 };
        if (/_pbh\b/.test(intrinsicName)) return { bits: 16, kind: 'bfloat', count: bytesTotal / 2 };
        if (/d$/.test(cType)) return { bits: 64, kind: 'float', count: bytesTotal / 8 };
        if (/h$/.test(cType)) return { bits: 16, kind: 'float', count: bytesTotal / 2 };
        if (/bh$/.test(cType)) return { bits: 16, kind: 'bfloat', count: bytesTotal / 2 };
        if (cType === '__m128' || cType === '__m256' || cType === '__m512') {
            return { bits: 32, kind: 'float', count: bytesTotal / 4 };
        }
        return { bits: 8, kind: 'uint', count: bytesTotal };
    }
    function intelLaneLiteral(v, info) {
        if (info.kind === 'int' || info.kind === 'uint') {
            return info.bits === 64 ? v + 'LL' : String(v);
        }
        if (info.kind === 'float') {
            if (info.bits === 32) return Number(v) + 'f';
            if (info.bits === 16) return '((__fp16)' + Number(v) + 'f)';
            return String(Number(v));
        }
        if (info.kind === 'bfloat') return '((__bf16)' + Number(v) + 'f)';
        return String(v);
    }
    function intelSetrCall(values, info) {
        const width = info.bits * info.count;
        const prefix = ({ 128: '_mm', 256: '_mm256', 512: '_mm512' })[width];
        const suffixMap = {
            'int|8':  'epi8',  'int|16':  'epi16',  'int|32':  'epi32',  'int|64':  'epi64x',
            'uint|8': 'epi8',  'uint|16': 'epi16',  'uint|32': 'epi32',  'uint|64': 'epi64x',
            'float|16': 'ph',  'float|32': 'ps',    'float|64': 'pd',
            'bfloat|16': 'pbh',
        };
        const suffix = suffixMap[info.kind + '|' + info.bits];
        if (!prefix || !suffix) {
            throw new Error('no setr builder for ' + info.kind + info.bits + ' x ' + width);
        }
        if (width === 128 && info.bits === 64) {
            // No _mm_setr_epi64x; use _set with reversed args.
            const rev = values.slice().reverse();
            return prefix + '_set_' + suffix + '(' +
                rev.map(v => intelLaneLiteral(v, info)).join(', ') + ')';
        }
        return prefix + '_setr_' + suffix + '(' +
            values.map(v => intelLaneLiteral(v, info)).join(', ') + ')';
    }

    function initList(values, typeName) {
        const info = laneInfo(typeName);
        const tup = tupleCount(typeName);
        if (tup > 0) {
            const subs = [];
            const n = values.length / tup;
            for (let k = 0; k < n; k++) {
                const sub = values.slice(k * tup, (k + 1) * tup);
                subs.push('{' + sub.map(v => laneLiteral(v, info)).join(',') + '}');
            }
            return '{{' + subs.join(',') + '}}';
        }
        return '{' + values.map(v => laneLiteral(v, info)).join(',') + '}';
    }

    // Build a small C++ source that exposes RESULT either as a folded
    // global (read out of .rodata by the asm parser) or by printing
    // its bytes from main() (CE's executor mode picks this up via stdout).
    //
    // We include `<cstdio>` + `main()` only when the target compiler
    // ships a C++ stdlib *and* execution makes sense -- that is, on
    // Intel via godbolt's cclang_trunk. The ARM cross-compiler on godbolt
    // (armv8-full-cclang-trunk) has no stdlib, so we keep the harness
    // header-free; the asm-parse path doesn't need printf.
    function buildFoldSource(rec, inputValues) {
        const cfg = window.SimdTooltips && window.SimdTooltips.ceConfigFor(rec);
        if (!cfg) throw new Error('no Compiler Explorer config for this intrinsic');
        const includes = cfg.headers.map(h => `#include <${h}>`).join('\n');
        const intel = isIntelIntrinsic(rec.name);

        const inputs = rec.example.inputs;
        const decls = [];
        const argParts = [];
        for (let i = 0; i < inputs.length; i++) {
            const inp = inputs[i];
            const vals = inputValues[i];
            const isImm = (intel && INTEL_IMM_TYPES.has(inp.type))
                || /^const\s+(?:unsigned\s+)?int$/.test(inp.type);
            if (isImm) {
                argParts.push(String(vals[0]));
                continue;
            }
            if (intel && INTEL_VEC_BYTES[inp.type] != null) {
                const info = intelLaneInfoFor(rec.name, inp.type, 'input');
                decls.push(`const ${inp.type} ${inp.name} = ${intelSetrCall(vals, info)};`);
            } else {
                decls.push(`const ${inp.type} ${inp.name} = ${initList(vals, inp.type)};`);
            }
            argParts.push(inp.name);
        }
        const argList = argParts.join(', ');
        const retType = rec.example.output.type;
        const intelTypedefs = intel
            ? '\n#if !defined(_MSC_VER)\ntypedef long long __int64;\ntypedef int __int32;\ntypedef short __int16;\ntypedef signed char __int8;\n#endif\n'
            : '';
        const stdioHeaders = intel ? '#include <cstdio>\n#include <cstddef>\n' : '';
        const mainFn = intel
            ? '\nint main() {\n' +
              '    const unsigned char* p = reinterpret_cast<const unsigned char*>(&RESULT);\n' +
              '    for (size_t i = 0; i < sizeof(RESULT); i++) std::printf("%02x", p[i]);\n' +
              '    return 0;\n' +
              '}\n'
            : '';
        return (
            includes + '\n' + stdioHeaders + intelTypedefs + '\n' +
            decls.join('\n') + '\n\n' +
            `extern "C" const ${retType} RESULT = ${rec.name}(${argList});\n` +
            mainFn
        );
    }

    // Parse `data.asm` (CE's structured asm output) and pull the bytes
    // that follow the `RESULT:` label until the next label or directive
    // we don't recognize. Returns a Uint8Array of `byteCount` bytes.
    function parseRodataBytes(asmLines, byteCount) {
        const text = asmLines.map(l => (typeof l === 'string' ? l : l.text || '')).join('\n');
        const lines = text.split('\n').map(s => s.trim());
        let i = lines.findIndex(l => /^_?RESULT:\s*$/.test(l));
        if (i < 0) throw new Error('RESULT label not found in asm');
        const out = [];
        for (i++; i < lines.length && out.length < byteCount; i++) {
            const t = lines[i];
            if (!t || t.startsWith('//') || t.startsWith('#')) continue;
            let m;
            if ((m = t.match(/^\.byte\s+(.+)$/))) {
                for (const v of m[1].split(',').map(s => s.trim())) {
                    out.push(parseInt(v, v.startsWith('0x') ? 16 : 10) & 0xff);
                }
                continue;
            }
            if ((m = t.match(/^\.short\s+(.+)$/)) || (m = t.match(/^\.hword\s+(.+)$/))) {
                for (const v of m[1].split(',').map(s => s.trim())) {
                    const u = parseInt(v, v.startsWith('0x') ? 16 : 10) & 0xffff;
                    out.push(u & 0xff, (u >>> 8) & 0xff);
                }
                continue;
            }
            if ((m = t.match(/^\.long\s+(.+)$/)) || (m = t.match(/^\.word\s+(.+)$/))) {
                for (const v of m[1].split(',').map(s => s.trim())) {
                    const u = parseInt(v, v.startsWith('0x') ? 16 : 10) >>> 0;
                    out.push(u & 0xff, (u >>> 8) & 0xff, (u >>> 16) & 0xff, (u >>> 24) & 0xff);
                }
                continue;
            }
            if ((m = t.match(/^\.quad\s+(.+)$/)) || (m = t.match(/^\.xword\s+(.+)$/))) {
                for (const v of m[1].split(',').map(s => s.trim())) {
                    let n = BigInt(v);
                    if (n < 0n) n = (1n << 64n) + n;
                    for (let k = 0; k < 8; k++) out.push(Number((n >> BigInt(k * 8)) & 0xffn));
                }
                continue;
            }
            if ((m = t.match(/^\.zero\s+(\d+)/))) {
                for (let k = 0; k < parseInt(m[1], 10); k++) out.push(0);
                continue;
            }
            if (/^\.(?:size|type|globl|p2align|align|section)/.test(t)) continue;
            if (/^[._A-Za-z]\w*:\s*$/.test(t)) break;  // next label
            if (t.startsWith('.')) break;               // unknown directive
        }
        if (out.length < byteCount) {
            throw new Error(
                `expected ${byteCount} bytes, parsed ${out.length}`
            );
        }
        return new Uint8Array(out.slice(0, byteCount));
    }

    function ceBase64Url(s) {
        return btoa(unescape(encodeURIComponent(s)))
            .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    }
    function ceClientstateUrl(rec, inputValues) {
        const cfg = window.SimdTooltips.ceConfigFor(rec);
        if (!cfg) return null;
        const source = buildFoldSource(rec, inputValues);
        // Stay on `language: 'c'`: godbolt's compiler IDs are
        // language-scoped, and our ARM/Intel compilers
        // (armv8-full-cclang-trunk, cclang_trunk, ...) are catalogued
        // under c. Setting c++ here makes godbolt drop our compiler and
        // fall back to its default c++ one (which is delightful but not
        // what we want). `-x c++` in the options keeps the compile in
        // C++ mode, where dynamic-init globals are legal.
        const state = {
            sessions: [{
                id: 1, language: 'c', source,
                compilers: [{
                    id: cfg.compiler, options: cfg.options + ' -x c++', libs: [],
                    filters: {
                        binary: false, commentOnly: true, demangle: true, directives: true,
                        execute: false, intel: false, labels: true, libraryCode: false, trim: false,
                    },
                }],
            }],
            version: 4,
        };
        return 'https://godbolt.org/clientstate/' + ceBase64Url(JSON.stringify(state));
    }

    // Mode is "fold" (read RESULT bytes out of .rodata in the asm) or
    // "execute" (CE's executor runs the binary; we parse stdout, which
    // is the hex-printed bytes from main()).
    //
    // Execute mode only applies when the target compiler on godbolt
    // ships both libstdc++ *and* an executor backend -- currently
    // Intel via cclang_trunk. ARM cross-compilers on godbolt have
    // neither, so we always try fold mode for ARM and let CE's
    // clang-trunk constant-fold what it can (which is more than our
    // local macOS arm64 clang does, e.g. table lookups).
    async function ceCompileFold(rec, inputValues) {
        const intel = isIntelIntrinsic(rec.name);
        const mode = (intel && rec.example.verified_via === 'execute')
            ? 'execute' : 'fold';
        const cfg = window.SimdTooltips && window.SimdTooltips.ceConfigFor(rec);
        if (!cfg) throw new Error('no Compiler Explorer config');
        const source = buildFoldSource(rec, inputValues);
        const resp = await fetch(
            'https://godbolt.org/api/compiler/' + encodeURIComponent(cfg.compiler) + '/compile',
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
                body: JSON.stringify({
                    source,
                    options: {
                        userArguments: cfg.options + ' -x c++',
                        filters: {
                            binary: false, commentOnly: true, demangle: false,
                            directives: false,
                            execute: mode === 'execute',
                            intel: false,
                            labels: true, libraryCode: false, trim: false,
                        },
                        compilerOptions: { executorRequest: mode === 'execute' },
                        libraries: [],
                    },
                    lang: 'c',
                }),
            }
        );
        if (!resp.ok) throw new Error('CE returned HTTP ' + resp.status);
        const data = await resp.json();
        if (mode === 'execute') {
            // The executor wraps the build+run; status lives at the
            // outer level (data.code) for build status and
            // data.execResult.code for run status.
            const exec = data.execResult || data;
            if (exec.buildResult && exec.buildResult.code !== 0) {
                const stderr = (exec.buildResult.stderr || []).map(l => l.text || l).join('\n');
                throw new Error('clang error: ' + stderr.slice(0, 240));
            }
            if (exec.code !== 0) {
                const stderr = (exec.stderr || []).map(l => l.text || l).join('\n');
                throw new Error('run error (exit ' + exec.code + '): ' + (stderr || '').slice(0, 240));
            }
            const out = (exec.stdout || []).map(l => l.text || l).join('').trim();
            if (!/^[0-9a-fA-F]+$/.test(out)) {
                throw new Error('unexpected stdout: ' + out.slice(0, 120));
            }
            const expected = expectedOutputBytes(rec);
            if (out.length < expected * 2) {
                throw new Error('expected ' + expected + ' bytes, got ' + (out.length / 2));
            }
            const bytes = new Uint8Array(expected);
            for (let i = 0; i < expected; i++) bytes[i] = parseInt(out.slice(i * 2, i * 2 + 2), 16);
            return { bytes, source };
        }
        if (data.code !== 0) {
            const stderr = (data.stderr || []).map(l => l.text || l).join('\n');
            throw new Error('clang error: ' + stderr.slice(0, 240));
        }
        const expected = expectedOutputBytes(rec);
        const bytes = parseRodataBytes(data.asm || [], expected);
        return { bytes, source };
    }

    function expectedOutputBytes(rec) {
        // We trust the cached output's byte length -- it tells us how
        // many bytes RESULT occupies for this intrinsic.
        return Math.floor(rec.example.output.bytes_hex.length / 2);
    }

    function decodeOutput(rec, bytes) {
        const out = rec.example.output;
        let bits, kind, count;
        // For Intel `__m128i` etc. the lane shape comes from the
        // intrinsic name suffix, not the C type.
        if (isIntelIntrinsic(rec.name) && INTEL_VEC_BYTES[out.type] != null) {
            const info = intelLaneInfoFor(rec.name, out.type, 'output');
            bits = info.bits; kind = info.kind; count = info.count;
        } else {
            ({ bits, kind } = laneInfo(out.type));
            count = bits ? (bytes.length * 8) / bits : 0;
        }
        if (!bits) return [];
        const values = [];
        const lb = bits / 8;
        for (let i = 0; i < count; i++) {
            const slice = bytes.slice(i * lb, (i + 1) * lb);
            values.push(decodeLaneBytes(slice, bits, kind));
        }
        return values;
    }

    // Pull the user's current input values out of the editable cells.
    function readLiveInputs(exNode, rec) {
        const inputs = rec.example.inputs;
        const out = inputs.map(inp =>
            new Array(Array.isArray(inp.values) ? inp.values.length : 1));
        for (const cell of exNode.querySelectorAll('.ex-val[data-row][data-lane]')) {
            const ri = +cell.dataset.row, li = +cell.dataset.lane;
            const dec = cell.querySelector('.ex-dec');
            if (!dec) continue;
            const text = dec.textContent.trim();
            const ti = laneInfo(inputs[ri].type);
            let v;
            if (ti.kind === 'float' || ti.kind === 'bfloat') {
                v = Number(text);
                if (Number.isNaN(v)) throw new Error(
                    inputs[ri].name + '[' + li + '] = "' + text + '" is not a number'
                );
            } else {
                if (!/^[+-]?\d+$/.test(text)) throw new Error(
                    inputs[ri].name + '[' + li + '] = "' + text + '" is not an integer'
                );
                v = parseInt(text, 10);
            }
            out[ri][li] = v;
        }
        return out;
    }

    function seeOnCE(exNode) {
        const params = new URLSearchParams(location.search);
        const name = params.get('intrinsic');
        const rec = name && records[name];
        if (!rec) return;
        try {
            const inputValues = readLiveInputs(exNode, rec);
            const url = ceClientstateUrl(rec, inputValues);
            if (url) window.open(url, '_blank', 'noopener');
        } catch (err) {
            const status = exNode.parentElement.querySelector('.ex-status');
            if (status) {
                status.textContent = String(err.message || err);
                status.className = 'ex-status is-error';
            }
        }
    }

    async function runOnCE(exNode) {
        // Find the rec via the URL (we're on /?intrinsic=NAME).
        const params = new URLSearchParams(location.search);
        const name = params.get('intrinsic');
        const rec = name && records[name];
        if (!rec) return;
        const status = exNode.parentElement.querySelector('.ex-status');
        if (status) {
            status.textContent = 'compiling on godbolt.org…';
            status.className = 'ex-status is-busy';
        }
        try {
            const inputValues = readLiveInputs(exNode, rec);
            const { bytes } = await ceCompileFold(rec, inputValues);
            const outValues = decodeOutput(rec, bytes);
            // Update the output row in place.
            updateOutputRow(exNode, rec, bytes, outValues);
            if (status) {
                status.textContent = 'verified on Compiler Explorer ✓';
                status.className = 'ex-status is-ok';
            }
        } catch (err) {
            if (status) {
                status.textContent = String(err.message || err);
                status.className = 'ex-status is-error';
            }
        }
    }

    function updateOutputRow(exNode, rec, bytes, outValues) {
        // Compute which output lanes actually changed so the renderer
        // can flag them (.ex-changed). Compare strings to side-step
        // BigInt vs Number coercion quirks.
        const oldValues = (rec.example.output && rec.example.output.values) || [];
        const changedOutputLanes = new Set();
        for (let i = 0; i < outValues.length; i++) {
            if (String(oldValues[i]) !== String(outValues[i])) {
                changedOutputLanes.add(i);
            }
        }

        const newOut = Object.assign({}, rec.example.output, {
            bytes_hex: Array.from(bytes).map(b =>
                b.toString(16).padStart(2, '0')).join(''),
            values: outValues,
        });
        const newExample = Object.assign({}, rec.example, { output: newOut });
        // Persist the user's currently-edited inputs into the rec so
        // re-render shows them (otherwise we'd snap back to cached vals).
        const liveInputs = readLiveInputs(exNode, rec);
        newExample.inputs = rec.example.inputs.map((inp, i) =>
            Object.assign({}, inp, { values: liveInputs[i] })
        );
        rec.example = newExample;
        const wrap = exNode.closest('.ex-wrap');
        _renderExampleViewable = isLiveViewable(rec);
        _renderExampleRunnable = isLiveRunnable(rec);
        try {
            (wrap || exNode).outerHTML = renderExample(
                newExample, rec.name, { changedOutputLanes }
            );
        } finally {
            _renderExampleViewable = false;
            _renderExampleRunnable = false;
        }
    }

    function decodeLaneBytes(bytes, bits, kind) {
        if (kind === 'float') {
            if (bits === 32) return new Float32Array(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + 4))[0];
            if (bits === 64) return new Float64Array(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + 8))[0];
            if (bits === 16 && typeof Float16Array !== 'undefined') {
                return new Float16Array(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + 2))[0];
            }
        }
        if (kind === 'bfloat' && bits === 16) {
            const buf = new ArrayBuffer(4);
            new Uint8Array(buf).set([0, 0, bytes[0], bytes[1]]);
            return new Float32Array(buf)[0];
        }
        if (bits <= 32) {
            let n = 0;
            for (let i = bytes.length - 1; i >= 0; i--) n = (n * 256) + bytes[i];
            if (kind === 'int' && (bytes[bytes.length - 1] & 0x80)) {
                n -= Math.pow(2, bits);
            }
            return n;
        }
        // 64-bit
        let big = 0n;
        for (let i = bytes.length - 1; i >= 0; i--) big = (big << 8n) | BigInt(bytes[i]);
        if (kind === 'int' && (bytes[bytes.length - 1] & 0x80)) {
            big -= 1n << BigInt(bits);
        }
        return big.toString();  // string for safe display past Number precision
    }
    // Per-render flags set by the dedicated /?intrinsic page. Both default
    // to false in compact-card / tooltip rendering.
    //   _renderExampleViewable -- inputs are editable + "see on CE" button
    //   _renderExampleRunnable -- additionally show "run on CE" button
    let _renderExampleViewable = false;
    let _renderExampleRunnable = false;

    // Lane info that's Intel-aware: for `__m128i` etc., the lane shape
    // comes from the intrinsic-name suffix, not the C type.
    function laneInfoFor(typeName, intrinsicName, context) {
        if (intrinsicName && isIntelIntrinsic(intrinsicName)
            && INTEL_VEC_BYTES[typeName] != null) {
            const info = intelLaneInfoFor(intrinsicName, typeName, context);
            return { bits: info.bits, kind: info.kind };
        }
        return laneInfo(typeName);
    }

    // `highlight` (optional): { changedOutputLanes: Set<number> } -- cells
    // in the output row whose values changed since the last render get
    // an `.ex-changed` class. Used right after a successful "run on CE".
    function renderExample(ex, intrinsicName, highlight) {
        const inputs = ex.inputs || [];
        const out = ex.output || {};
        const outVals = Array.isArray(out.values) ? out.values : [out.values];

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
        for (let ri = 0; ri < inputs.length; ri++) {
            const inp = inputs[ri];
            const { bits, kind } = laneInfoFor(inp.type, intrinsicName, 'input');
            const vals = Array.isArray(inp.values) ? inp.values : [inp.values];
            // Pointer params (loads) show what the intrinsic *reads from*
            // memory, not the pointer itself -- use *name to make that
            // clear.
            const isPtr = /\*\s*$/.test(inp.type || '');
            const labelName = (isPtr ? '*' : '') + (inp.name || '');
            rows.push({
                label: labelName + ':',
                values: vals,
                hexes: vals.map(v => hexFromValue(v, bits, kind)),
                cls: '',
                tupleCount: tupleCount(inp.type),
                rowIdx: ri,
            });
        }
        {
            const { bits, kind } = laneInfoFor(out.type, intrinsicName, 'output');
            const hexes = outVals.map((v, i) =>
                out.bytes_hex && bits ? hexFromBytes(out.bytes_hex, i, bits) : hexFromValue(v, bits, kind)
            );
            rows.push({
                label: '→', values: outVals, hexes,
                cls: 'ex-out', isOut: true,
                tupleCount: tupleCount(out.type),
            });
        }

        // Sub-vector boundaries are per-row: only register tuple rows
        // (int8x16x2_t, etc.) get the thicker separator at lane k * count.
        // Memory rows (pointer types) and plain vectors get no boundary.

        const lanes = Math.max(1, ...rows.map(r => r.values.length));
        const cells = [];
        for (const row of rows) {
            const labelClass = row.isOut ? 'ex-lbl ex-arrow' : 'ex-lbl';
            cells.push(`<span class="${labelClass}">${escapeHtml(row.label)}</span>`);
            for (let i = 0; i < lanes; i++) {
                const isBoundary = row.tupleCount > 0
                    && i > 0 && (i % row.tupleCount) === 0;
                const cls = ['ex-val', row.cls, isBoundary ? 'ex-boundary' : '']
                    .filter(Boolean).join(' ');
                if (i < row.values.length) {
                    const dec = String(row.values[i]);
                    const hex = row.hexes[i] || '';
                    // Inputs become contenteditable on the live-edit page.
                    const editable = _renderExampleViewable && !row.isOut
                        ? ` contenteditable="true" spellcheck="false"`
                        : '';
                    const dataAttrs = _renderExampleViewable && !row.isOut
                        ? ` data-row="${row.rowIdx}" data-lane="${i}"`
                        : '';
                    const changed = row.isOut && highlight
                        && highlight.changedOutputLanes
                        && highlight.changedOutputLanes.has(i)
                        ? ' ex-changed' : '';
                    cells.push(
                        `<span class="${cls}${changed}"${dataAttrs}>` +
                        `<span class="ex-dec"${editable}>${escapeHtml(dec)}</span>` +
                        `<span class="ex-hexcell">${escapeHtml(hex)}</span>` +
                        `</span>`
                    );
                } else {
                    cells.push(`<span class="${cls}"></span>`);
                }
            }
        }
        const cols = `auto repeat(${lanes}, max-content)`;
        const toggle =
            `<div class="ex-modes" role="group" aria-label="number base">` +
            `<button type="button" class="ex-mode is-active" data-mode="dec" aria-pressed="true">dec</button>` +
            `<button type="button" class="ex-mode" data-mode="hex" aria-pressed="false">hex</button>` +
            `</div>`;
        const runBtn = _renderExampleRunnable
            ? `<button type="button" class="ex-run" title="Re-compile on Compiler Explorer">↻ run on CE</button>`
            : '';
        const seeBtn = _renderExampleViewable
            ? `<button type="button" class="ex-see" title="Open the harness in Compiler Explorer">↗ see on CE</button>`
            : '';
        const status = _renderExampleViewable
            ? `<div class="ex-status" role="status"></div>`
            : '';
        const hint = _renderExampleViewable
            ? (_renderExampleRunnable
                ? `<div class="ex-hint"><em>input values are editable</em> — change a number and click <strong>↻ run on CE</strong> to recompile (or <strong>↗ see on CE</strong> to inspect the harness).</div>`
                : `<div class="ex-hint"><em>input values are editable</em> — click <strong>↗ see on CE</strong> to inspect / recompile the harness.</div>`)
            : '';
        // Wrap so updateOutputRow can replace the entire example block
        // (hint + panel + buttons + status) atomically without leaving
        // stragglers.
        return (
            `<div class="ex-wrap">` + hint +
            `<div class="ex" style="grid-template-columns:${cols}">${toggle}${cells.join('')}</div>` +
            runBtn + seeBtn + status +
            `</div>`
        );
    }
})();
