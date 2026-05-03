/* simd.dev landing page — search box + result card.
 *
 * Loads simd-names.json eagerly (small, ~95 KB gzipped) so search lights
 * up immediately. Loads simd-data.json lazily on first selection. The
 * simd-tooltips.js library is also on the page for in-prose hover.
 */

(function () {
    'use strict';

    const NAMES_URL = 'dist/simd-names.json';
    const DATA_URL  = 'dist/simd-data.json';
    const MAX_RESULTS = 80;

    const $q       = document.getElementById('q');
    const $status  = document.getElementById('search-status');
    const $results = document.getElementById('results');
    const $card    = document.getElementById('card');

    let allNames = null;       // [string]
    let typeSet = null;        // Set<string>
    let records = null;        // {name: rec}    -- loaded lazily
    let dataPromise = null;

    init().catch(err => {
        $status.textContent = 'failed to load names index';
        console.error('simd.dev load failure:', err);
    });

    async function init() {
        $status.textContent = 'loading…';
        const namesDoc = await fetchJSON(NAMES_URL);
        allNames = namesDoc.names || [];
        typeSet = new Set(namesDoc.types || []);
        $status.textContent = allNames.length.toLocaleString() + ' indexed';

        $q.addEventListener('input', onInput);
        $q.addEventListener('keydown', onKeydown);
        $results.addEventListener('click', onResultsClick);
        document.querySelectorAll('code[data-q]').forEach(c => {
            c.style.cursor = 'pointer';
            c.addEventListener('click', () => {
                $q.value = c.dataset.q;
                $q.focus();
                renderResults(c.dataset.q);
            });
        });

        // If the URL has #intrinsic, jump to it.
        if (location.hash.length > 1) {
            const target = decodeURIComponent(location.hash.slice(1));
            if (allNames.includes(target)) {
                $q.value = target;
                renderResults(target);
                showCard(target);
            }
        }

        $q.focus();
    }

    function onInput() { renderResults($q.value); }

    function onKeydown(e) {
        if (e.key === 'Enter') {
            const first = $results.querySelector('button');
            if (first) { first.click(); e.preventDefault(); }
        } else if (e.key === 'Escape') {
            $q.value = '';
            renderResults('');
            $card.hidden = true;
        }
    }

    function renderResults(query) {
        const q = (query || '').trim().toLowerCase();
        $results.innerHTML = '';
        if (!q) return;

        // Cheap substring match. Prefer prefix matches first, then substring.
        const exact = [];
        const prefix = [];
        const sub = [];
        for (const n of allNames) {
            const ln = n.toLowerCase();
            if (ln === q) exact.push(n);
            else if (ln.startsWith(q)) prefix.push(n);
            else if (ln.indexOf(q) >= 0) sub.push(n);
            if (exact.length + prefix.length + sub.length > MAX_RESULTS * 3) break;
        }
        const ranked = [...exact, ...prefix, ...sub].slice(0, MAX_RESULTS);

        if (ranked.length === 0) {
            const li = document.createElement('li');
            li.innerHTML = '<span class="muted" style="padding:3px 6px;">no matches</span>';
            $results.appendChild(li);
            return;
        }

        const frag = document.createDocumentFragment();
        for (const n of ranked) {
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

    async function showCard(name) {
        try {
            const recs = await ensureRecords();
            const rec = recs[name];
            if (!rec) {
                $card.innerHTML = `<div class="muted"><code>${escapeHtml(name)}</code> is in the index but has no record (likely an ambiguous overload — try a typed variant).</div>`;
            } else {
                $card.innerHTML = renderCardHTML(name, rec);
                history.replaceState(null, '', '#' + encodeURIComponent(name));
            }
            $card.hidden = false;
            $card.classList.toggle('is-type', !!(rec && rec.kind === 'type'));
            $card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        } catch (err) {
            $card.innerHTML = `<div class="muted">load failed: ${escapeHtml(String(err.message || err))}</div>`;
            $card.hidden = false;
        }
    }

    function ensureRecords() {
        if (records) return Promise.resolve(records);
        if (dataPromise) return dataPromise;
        $status.textContent = 'loading details…';
        dataPromise = fetchJSON(DATA_URL).then(doc => {
            records = doc.records || doc;
            $status.textContent = allNames.length.toLocaleString() + ' indexed';
            return records;
        });
        return dataPromise;
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

        const linkLabel = rec.source === 'arm-acle'
            ? 'Arm developer docs →'
            : 'Intel Intrinsics Guide →';
        const links = [];
        if (rec.doc_url) {
            links.push(`<a class="upstream-link" href="${escapeAttr(rec.doc_url)}" target="_blank" rel="noopener">${linkLabel}</a>`);
        }
        // Compiler Explorer link, computed via the simd-tooltips library helper.
        const ceUrl = (window.SimdTooltips && window.SimdTooltips.compilerExplorerUrl)
            ? window.SimdTooltips.compilerExplorerUrl(rec)
            : null;
        if (ceUrl) {
            links.push(`<a class="upstream-link" href="${escapeAttr(ceUrl)}" target="_blank" rel="noopener">Compiler Explorer →</a>`);
        }
        const link = links.length ? `<div class="upstream-links">${links.join(' &middot; ')}</div>` : '';

        const desc = rec.description
            ? `<div class="description">${escapeHtml(rec.description)}</div>`
            : '';

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

    // ---- helpers ----

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
