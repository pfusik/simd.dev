/**
 * simd-tooltips.js — drop-in tooltip library for SIMD intrinsic names.
 *
 * Default mode (lazy): listens for mousemove globally; when the cursor is over
 * a word that matches a known SIMD intrinsic, pops a tooltip with its
 * signature and a short description. The DOM is never mutated.
 *
 * Optional mode (wrap): walks the page once at init, wraps every intrinsic
 * in <span class="simd-intrinsic"> with tabIndex=0, gives keyboard users a
 * focusable element and a visible underline. Set `wrap: true`.
 *
 * Usage (auto-init, default lazy mode):
 *
 *   <script src="/path/to/simd-tooltips.js" defer></script>
 *
 * Usage (auto-init with options on the script tag):
 *
 *   <script src="/path/to/simd-tooltips.js"
 *           data-scope="article"
 *           data-on="click"
 *           data-wrap                 // opt into DOM-walk + wrap-in-span mode
 *           data-base-url="/path/to/"
 *           defer></script>
 *
 * Usage (manual init):
 *
 *   <script src="/path/to/simd-tooltips.js" data-no-auto defer></script>
 *   <script>
 *     SimdTooltips.init({
 *       scope: '.code',              // only meaningful in wrap mode
 *       on: 'hover',                 // 'hover' | 'click'
 *       wrap: false,                 // default: lazy detection (no DOM walk)
 *       baseUrl: '/path/to/',
 *       names: {...},                // optional: pre-supplied names index
 *       data: {...},                 // optional: pre-supplied records
 *     }).then(() => console.log('ready'));
 *   </script>
 *
 * License: same as the simd.dev project.
 */
(function (root) {
  'use strict';

  const VERSION = '0.1.0';

  const DEFAULTS = {
    scope: 'body',
    on: 'hover',         // 'hover' | 'click' | 'hover+?'  (lazy only for 'hover+?')
    wrap: false,         // false = lazy hover detection, true = walk and wrap
    baseUrl: scriptDir(),
    namesUrl: null,      // defaults to baseUrl + 'simd-names.json'
    dataUrl: null,       // defaults to baseUrl + 'simd-data.json'
    names: null,         // pre-supplied (skip fetch)
    data: null,          // pre-supplied (skip fetch)
    skipSelector: 'script,style,textarea,input,select,option,.simd-skip',
    wrapClass: 'simd-intrinsic',
    tooltipClass: 'simd-tooltip',
    moveThrottleMs: 30,  // throttle for lazy-mode mousemove handler
  };

  // ---------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------
  let cfg = null;
  let nameSet = null;            // Set<string> for O(1) token lookup
  let typeSet = null;            // Set<string> -- subset of names that are SIMD types
  let ambiguous = null;          // {alias: [canonical, ...]}
  let records = null;            // {name: record} -- lazy
  let dataPromise = null;        // Promise<records> in flight
  let activeTooltip = null;
  let activeTarget = null;       // current trigger element (or virtual key)
  let activeKey = null;          // string id of the current source word
  let hideTimer = null;
  let lastMoveTs = 0;
  let attachedListeners = [];    // [{target, type, fn, opts}, ...] for clean re-init
  let activeHint = null;         // {el, word, rect} -- "hover+?" mode badge
  let hintHideTimer = null;

  // ---------------------------------------------------------------------
  // Public API
  // ---------------------------------------------------------------------
  const SimdTooltips = {
    version: VERSION,
    init: init,
    scan: scan,                  // wrap-mode only: re-scan a subtree (SPAs)
    unwrap: unwrap,              // wrap-mode only: remove all wrappers
    hide: hide,
    _state: () => ({ cfg, nameCount: nameSet ? nameSet.size : 0, recordCount: records ? Object.keys(records).length : 0 }),
  };

  // ---------------------------------------------------------------------
  // Init
  // ---------------------------------------------------------------------
  async function init(opts) {
    // Tear down any previous configuration so re-init (e.g. switching trigger
    // mode) doesn't stack handlers on top of the old ones.
    detachListeners();
    hide();

    cfg = Object.assign({}, DEFAULTS, opts || {});
    if (!cfg.namesUrl) cfg.namesUrl = joinUrl(cfg.baseUrl, 'simd-names.json');
    if (!cfg.dataUrl) cfg.dataUrl = joinUrl(cfg.baseUrl, 'simd-data.json');

    injectStyles();

    // Names: load eagerly (needed for both modes).
    let namesDoc;
    if (cfg.names) {
      namesDoc = cfg.names;
    } else {
      namesDoc = await fetchJSON(cfg.namesUrl);
    }
    nameSet = new Set(namesDoc.names || []);
    typeSet = new Set(namesDoc.types || []);
    ambiguous = namesDoc.ambiguous || {};

    // Data: defer. If pre-supplied, use immediately.
    if (cfg.data) {
      records = cfg.data.records || cfg.data;
    }

    if (cfg.wrap) scan(cfg.scope);
    attachListeners();
    return SimdTooltips;
  }

  // ---------------------------------------------------------------------
  // Listeners
  // ---------------------------------------------------------------------
  function listen(target, type, fn, opts) {
    target.addEventListener(type, fn, opts);
    attachedListeners.push({ target, type, fn, opts });
  }

  function detachListeners() {
    for (const { target, type, fn, opts } of attachedListeners) {
      target.removeEventListener(type, fn, opts);
    }
    attachedListeners = [];
  }

  function attachListeners() {
    if (cfg.wrap) {
      if (cfg.on === 'click') {
        listen(document, 'click', onWrapClick, true);
        // No focus listeners in click mode -- a mousedown on a tabIndex-0
        // span fires focus before click, which would briefly show the tooltip
        // and then the click would toggle it off again. Keyboard users still
        // get coverage via the Enter/Space handler in onKeydown.
      } else {
        listen(document, 'mouseover', onWrapEnter, true);
        listen(document, 'mouseout', onWrapLeave, true);
        listen(document, 'focusin', onWrapEnter, true);
        listen(document, 'focusout', onWrapLeave, true);
      }
    } else {
      // Lazy modes
      if (cfg.on === 'click') {
        listen(document, 'click', onLazyClick, true);
      } else if (cfg.on === 'hover+?') {
        // Hover surfaces a "?" badge; click on the word or the badge opens
        // the full tooltip.
        listen(document, 'mousemove', onLazyMove, true);
        listen(document, 'click', onLazyClick, true);
        listen(document, 'mouseleave', onDocLeave, true);
      } else {
        // Default 'hover': full tooltip on hover.
        listen(document, 'mousemove', onLazyMove, true);
        listen(document, 'mouseleave', onDocLeave, true);
      }
    }
    listen(document, 'keydown', onKeydown, true);
    listen(window, 'scroll', repositionOrHide, { passive: true, capture: true });
    listen(window, 'resize', repositionOrHide, { passive: true });
  }

  function onDocLeave() { scheduleHide(); }

  // ---------------------------------------------------------------------
  // Lazy mode: detect word under cursor without DOM mutation
  // ---------------------------------------------------------------------
  function onLazyMove(ev) {
    const now = performance.now();
    if (now - lastMoveTs < cfg.moveThrottleMs) return;
    lastMoveTs = now;
    detectAndShow(ev.clientX, ev.clientY);
  }

  function onLazyClick(ev) {
    // Clicks inside the open tooltip should pass through (so links work).
    if (activeTooltip && activeTooltip.contains(ev.target)) return;
    // Clicks on the hint badge are handled by the badge's own listener.
    if (activeHint && activeHint.el && activeHint.el.contains(ev.target)) return;
    if (!detectAndShow(ev.clientX, ev.clientY, /*click=*/true)) {
      hide();
      removeHint();
    } else {
      ev.preventDefault();
    }
  }

  function detectAndShow(clientX, clientY, click) {
    // If the pointer is over our tooltip, leave it alone. Use elementFromPoint
    // + contains() rather than rect math so it's robust regardless of layout
    // (children with overflow, transforms, etc. still resolve correctly).
    if (isOverActiveTooltip(clientX, clientY)) {
      if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
      return false;
    }
    if (isOverHint(clientX, clientY)) {
      if (hintHideTimer) { clearTimeout(hintHideTimer); hintHideTimer = null; }
      return false;
    }

    if (cfg.on === 'hover+?') return detectHoverHint(clientX, clientY, click);

    const found = wordAtPoint(clientX, clientY);
    if (!found) {
      if (!click) scheduleHide();
      return false;
    }
    const { word, rect } = found;
    const key = word + '@' + Math.round(rect.left) + ',' + Math.round(rect.top);
    if (key === activeKey) return true;
    activeKey = key;
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    showAtRect(word, rect);
    return true;
  }

  function detectHoverHint(x, y, click) {
    const found = wordAtPoint(x, y);
    if (click) {
      // Click on a word: open full tooltip and clear the hint.
      if (found) {
        removeHint();
        const key = found.word + '@' + Math.round(found.rect.left) + ',' + Math.round(found.rect.top);
        activeKey = key;
        if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
        showAtRect(found.word, found.rect);
        return true;
      }
      return false;  // click outside: caller will hide()
    }
    // Mouse move:
    if (!found) {
      scheduleRemoveHint();
      return false;
    }
    if (!activeHint || activeHint.word !== found.word) {
      showHint(found.word, found.rect);
    }
    return true;
  }

  function isOverActiveTooltip(x, y) {
    if (!activeTooltip || activeTooltip.style.display === 'none') return false;
    const e = document.elementFromPoint(x, y);
    return !!(e && activeTooltip.contains(e));
  }

  function isOverHint(x, y) {
    if (!activeHint || !activeHint.el || activeHint.el.style.display === 'none') return false;
    const e = document.elementFromPoint(x, y);
    return !!(e && activeHint.el.contains(e));
  }

  function ensureHintEl() {
    if (activeHint && activeHint.el && activeHint.el.isConnected) return activeHint.el;
    const el = document.createElement('span');
    el.className = 'simd-hint';
    el.setAttribute('role', 'button');
    el.setAttribute('aria-label', 'Show intrinsic info');
    el.tabIndex = 0;
    el.textContent = '?';
    document.body.appendChild(el);
    el.addEventListener('mouseenter', () => {
      if (hintHideTimer) { clearTimeout(hintHideTimer); hintHideTimer = null; }
    });
    el.addEventListener('mouseleave', scheduleRemoveHint);
    el.addEventListener('click', (ev) => {
      ev.stopPropagation();
      ev.preventDefault();
      if (!activeHint) return;
      const { word, rect } = activeHint;
      removeHint();
      activeKey = word + '@' + Math.round(rect.left) + ',' + Math.round(rect.top);
      if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
      showAtRect(word, rect);
    });
    return el;
  }

  function showHint(word, rect) {
    const el = ensureHintEl();
    el.style.display = 'inline-block';
    // Position: just to the right of the word, top-aligned. Fall back to
    // left-of when there's no room on the right.
    el.style.left = '0px';
    el.style.top = '0px';
    const hr = el.getBoundingClientRect();
    let left = rect.right + 2;
    if (left + hr.width + 4 > window.innerWidth) left = rect.left - hr.width - 2;
    if (left < 4) left = 4;
    let top = rect.top + (rect.height - hr.height) / 2;  // vertically centered with word
    if (top < 4) top = 4;
    if (top + hr.height + 4 > window.innerHeight) top = window.innerHeight - hr.height - 4;
    el.style.left = Math.round(left + window.scrollX) + 'px';
    el.style.top = Math.round(top + window.scrollY) + 'px';
    if (hintHideTimer) { clearTimeout(hintHideTimer); hintHideTimer = null; }
    activeHint = { el, word, rect };
  }

  function scheduleRemoveHint() {
    if (hintHideTimer) clearTimeout(hintHideTimer);
    hintHideTimer = setTimeout(removeHint, 150);
  }

  function removeHint() {
    if (hintHideTimer) { clearTimeout(hintHideTimer); hintHideTimer = null; }
    if (activeHint && activeHint.el) activeHint.el.style.display = 'none';
    activeHint = null;
  }

  function wordAtPoint(x, y) {
    const elt = document.elementFromPoint(x, y);
    if (!elt) return null;
    if (cfg.skipSelector && elt.closest(cfg.skipSelector)) return null;
    if (activeTooltip && activeTooltip.contains(elt)) return null;

    const pos = caretFromPoint(x, y);
    if (!pos) return null;
    const node = pos.node;
    const offset = pos.offset;
    if (!node || node.nodeType !== Node.TEXT_NODE) return null;
    const text = node.nodeValue;
    if (!text) return null;

    let s = offset;
    let e = offset;
    while (s > 0 && /[A-Za-z0-9_]/.test(text[s - 1])) s--;
    while (e < text.length && /[A-Za-z0-9_]/.test(text[e])) e++;
    if (s === e) return null;
    const word = text.slice(s, e);
    if (!nameSet.has(word)) return null;

    const range = document.createRange();
    try {
      range.setStart(node, s);
      range.setEnd(node, e);
    } catch (_) { return null; }
    const rect = range.getBoundingClientRect();
    return { word, rect };
  }

  function caretFromPoint(x, y) {
    if (document.caretPositionFromPoint) {
      const p = document.caretPositionFromPoint(x, y);
      return p ? { node: p.offsetNode, offset: p.offset } : null;
    }
    if (document.caretRangeFromPoint) {
      const r = document.caretRangeFromPoint(x, y);
      return r ? { node: r.startContainer, offset: r.startOffset } : null;
    }
    return null;
  }

  // ---------------------------------------------------------------------
  // Wrap mode (optional): scan + listen on wrapped spans
  // ---------------------------------------------------------------------
  function scan(scope) {
    if (!nameSet) return;
    const root = resolveRoot(scope);
    if (!root) return;

    const skipNodes = (() => {
      try { return new Set(root.querySelectorAll(cfg.skipSelector)); } catch (_) { return new Set(); }
    })();

    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        const p = node.parentElement;
        if (!p) return NodeFilter.FILTER_REJECT;
        if (p.classList && p.classList.contains(cfg.wrapClass)) return NodeFilter.FILTER_REJECT;
        for (let n = p; n && n !== root; n = n.parentElement) {
          if (skipNodes.has(n)) return NodeFilter.FILTER_REJECT;
        }
        return NodeFilter.FILTER_ACCEPT;
      },
    });

    const TOKEN = /[A-Za-z_][A-Za-z0-9_]*/g;
    const todo = [];
    let node;
    while ((node = walker.nextNode())) {
      const text = node.nodeValue;
      if (!text || text.length < 3) continue;
      let m, hits = null;
      TOKEN.lastIndex = 0;
      while ((m = TOKEN.exec(text)) !== null) {
        if (nameSet.has(m[0])) {
          (hits = hits || []).push({ start: m.index, end: m.index + m[0].length, name: m[0] });
        }
      }
      if (hits) todo.push({ node, text, hits });
    }

    for (const { node, text, hits } of todo) {
      const frag = document.createDocumentFragment();
      let cursor = 0;
      for (const h of hits) {
        if (h.start > cursor) frag.appendChild(document.createTextNode(text.slice(cursor, h.start)));
        const isType = typeSet.has(h.name);
        const span = document.createElement('span');
        // Both kinds keep `cfg.wrapClass` so the existing event handlers and
        // detach paths still work. Types add a `simd-type` modifier class for
        // distinct styling and are NOT tabbable -- intrinsics are the primary
        // navigation target; types are reference info.
        span.className = isType ? (cfg.wrapClass + ' simd-type') : cfg.wrapClass;
        span.dataset.name = h.name;
        if (!isType) span.tabIndex = 0;
        span.textContent = h.name;
        frag.appendChild(span);
        cursor = h.end;
      }
      if (cursor < text.length) frag.appendChild(document.createTextNode(text.slice(cursor)));
      node.parentNode.replaceChild(frag, node);
    }
  }

  function onWrapEnter(ev) {
    const el = closestWrapped(ev.target);
    if (!el) return;
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    if (activeTarget === el) return;
    activeTarget = el;
    activeKey = 'wrap:' + el.dataset.name + ':' + (el._uid || (el._uid = ++_uidCounter));
    showAtRect(el.dataset.name, el.getBoundingClientRect());
  }

  function onWrapLeave(ev) {
    const fromEl = closestWrapped(ev.target);
    if (!fromEl) return;
    if (ev.relatedTarget && activeTooltip && activeTooltip.contains(ev.relatedTarget)) return;
    const toEl = ev.relatedTarget && closestWrapped(ev.relatedTarget);
    if (toEl === fromEl) return;
    scheduleHide();
  }

  function onWrapClick(ev) {
    const el = closestWrapped(ev.target);
    if (!el) { hide(); return; }
    ev.preventDefault();
    if (activeTarget === el) hide();
    else onWrapEnter(ev);
  }

  function closestWrapped(target) {
    if (!target || !target.closest) return null;
    return target.closest('.' + cfg.wrapClass);
  }

  let _uidCounter = 0;

  // ---------------------------------------------------------------------
  // Keyboard
  // ---------------------------------------------------------------------
  function onKeydown(ev) {
    if (ev.key === 'Escape' && activeTooltip && activeTooltip.style.display !== 'none') {
      hide();
      if (activeTarget && activeTarget.focus) activeTarget.focus();
      return;
    }
    // Click-mode keyboard equivalent: Enter or Space on a focused wrapped span.
    if (cfg && cfg.wrap && cfg.on === 'click' && (ev.key === 'Enter' || ev.key === ' ')) {
      const el = document.activeElement;
      if (el && el.classList && el.classList.contains(cfg.wrapClass)) {
        ev.preventDefault();
        if (activeTarget === el) {
          hide();
        } else {
          activeTarget = el;
          activeKey = 'wrap:' + el.dataset.name + ':' + (el._uid || (el._uid = ++_uidCounter));
          showAtRect(el.dataset.name, el.getBoundingClientRect());
        }
      }
    }
  }

  // ---------------------------------------------------------------------
  // Tooltip rendering and positioning
  // ---------------------------------------------------------------------
  async function showAtRect(name, rect) {
    const tip = ensureTooltip();
    // Pre-mark the tooltip as a type if we know it before the data loads, so
    // the placeholder is already coloured correctly.
    tip.classList.toggle('simd-tooltip--type', !!(typeSet && typeSet.has(name)));
    tip.innerHTML = renderPlaceholder(name);
    tip.style.visibility = 'hidden';
    tip.style.display = 'block';
    positionAtRect(tip, rect);
    tip.style.visibility = 'visible';

    try {
      const recordsMap = await ensureRecords();
      // Race check: the user may have moved on while data was loading.
      if (activeKey == null) return;
      const rec = recordsMap[name];
      const ambig = ambiguous[name];
      tip.classList.toggle('simd-tooltip--type', !!(rec && rec.kind === 'type'));
      tip.innerHTML = renderTooltip(name, rec, ambig);
      positionAtRect(tip, rect);
    } catch (e) {
      tip.innerHTML = renderError(name, e);
      positionAtRect(tip, rect);
    }
  }

  function scheduleHide() {
    if (hideTimer) clearTimeout(hideTimer);
    hideTimer = setTimeout(hide, 150);
  }

  function hide() {
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    if (activeTooltip) activeTooltip.style.display = 'none';
    activeTarget = null;
    activeKey = null;
    removeHint();
  }

  function ensureTooltip() {
    if (activeTooltip) return activeTooltip;
    const tip = document.createElement('div');
    tip.className = cfg.tooltipClass;
    tip.setAttribute('role', 'tooltip');
    document.body.appendChild(tip);
    tip.addEventListener('mouseenter', () => { if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; } });
    tip.addEventListener('mouseleave', scheduleHide);
    activeTooltip = tip;
    return tip;
  }

  function repositionOrHide() {
    if (!activeTooltip || activeTooltip.style.display === 'none') return;
    // For wrap mode we have an actual target rect.
    if (activeTarget && activeTarget.getBoundingClientRect) {
      const r = activeTarget.getBoundingClientRect();
      if (r.bottom < 0 || r.top > window.innerHeight) { hide(); return; }
      positionAtRect(activeTooltip, r);
      return;
    }
    // Lazy mode: cursor likely moved already; hide on scroll/resize.
    hide();
  }

  function positionAtRect(tip, r) {
    // Default placement: to the *right* of the trigger word, top-aligned.
    // This keeps following lines of code visible (a tooltip below would cover
    // them). Fall back to left-of when there's no horizontal room.
    tip.style.left = '0px';
    tip.style.top = '0px';
    const maxW = Math.min(560, window.innerWidth - 24);
    tip.style.maxWidth = maxW + 'px';
    const tr = tip.getBoundingClientRect();
    const margin = 8;

    // Horizontal: prefer right-of, fall back to left-of, then clamp to viewport.
    let left = r.right + margin;
    if (left + tr.width + margin > window.innerWidth) {
      const leftAlt = r.left - tr.width - margin;
      if (leftAlt >= margin) {
        left = leftAlt;
      } else {
        // No room either side -- pick whichever side has more space and clamp.
        const spaceRight = window.innerWidth - r.right;
        const spaceLeft = r.left;
        left = spaceRight >= spaceLeft
          ? Math.max(margin, window.innerWidth - tr.width - margin)
          : Math.max(margin, r.left - tr.width - margin);
      }
    }

    // Vertical: top-align with the word; clamp inside viewport.
    let top = r.top;
    if (top + tr.height + margin > window.innerHeight) top = window.innerHeight - tr.height - margin;
    if (top < margin) top = margin;

    tip.style.left = Math.round(left + window.scrollX) + 'px';
    tip.style.top = Math.round(top + window.scrollY) + 'px';
  }

  function renderPlaceholder(name) {
    return `<div class="simd-tt-head"><code>${escapeHtml(name)}</code></div><div class="simd-tt-loading">loading…</div>`;
  }

  function renderError(name, err) {
    return `<div class="simd-tt-head"><code>${escapeHtml(name)}</code></div><div class="simd-tt-error">${escapeHtml(String(err && err.message || err || 'load failed'))}</div>`;
  }

  function renderTooltip(name, rec, ambig) {
    if (!rec && ambig) {
      const list = ambig.slice(0, 6).map(n => `<code>${escapeHtml(n)}</code>`).join(', ');
      const more = ambig.length > 6 ? ` <em>+${ambig.length - 6} more</em>` : '';
      return `<div class="simd-tt-head"><code>${escapeHtml(name)}</code> <span class="simd-tt-tag">overloaded</span></div>
        <div class="simd-tt-body">Resolves to ${ambig.length} typed variants: ${list}${more}</div>`;
    }
    if (!rec) return renderError(name, 'not in database');

    const families = (rec.family || []).map(f => `<span class="simd-tt-tag">${escapeHtml(f)}</span>`).join(' ');
    const archs = (rec.arch || []).map(a => `<span class="simd-tt-arch">${escapeHtml(a)}</span>`).join(' ');
    const desc = rec.description ? `<div class="simd-tt-desc">${escapeHtml(rec.description)}</div>` : '';
    const link = rec.doc_url ? `<div class="simd-tt-foot"><a href="${escapeAttr(rec.doc_url)}" target="_blank" rel="noopener">${rec.source === 'arm-acle' ? 'Arm developer docs' : 'Intel Intrinsics Guide'} →</a></div>` : '';

    // SIMD types use a slimmer layout: the "definition" is a one-line typedef
    // and lives next to the description. Intrinsics keep the multi-line <pre>
    // signature block.
    if (rec.kind === 'type') {
      const kindBadge = `<span class="simd-tt-kind">type</span>`;
      const def = rec.definition ? `<code class="simd-tt-typedef">${escapeHtml(rec.definition)}</code>` : '';
      return `<div class="simd-tt-head"><code>${escapeHtml(name)}</code> ${kindBadge} ${families} ${archs}</div>
        ${def}
        ${desc}
        ${link}`;
    }

    return `<div class="simd-tt-head"><code>${escapeHtml(name)}</code> ${families} ${archs}</div>
      <pre class="simd-tt-sig">${escapeHtml(rec.definition || '')}</pre>
      ${desc}
      ${link}`;
  }

  // ---------------------------------------------------------------------
  // Data loading
  // ---------------------------------------------------------------------
  function ensureRecords() {
    if (records) return Promise.resolve(records);
    if (dataPromise) return dataPromise;
    dataPromise = fetchJSON(cfg.dataUrl).then(doc => {
      records = doc.records || doc;
      return records;
    });
    return dataPromise;
  }

  async function fetchJSON(url) {
    const resp = await fetch(url, { credentials: 'omit' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status} for ${url}`);
    return resp.json();
  }

  // ---------------------------------------------------------------------
  // Cleanup
  // ---------------------------------------------------------------------
  function unwrap(scope) {
    const root = resolveRoot(scope || 'body');
    if (!root) return;
    const klass = cfg ? cfg.wrapClass : 'simd-intrinsic';
    for (const w of root.querySelectorAll('.' + klass)) {
      w.parentNode.replaceChild(document.createTextNode(w.textContent), w);
    }
  }

  // ---------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------
  function resolveRoot(scope) {
    if (!scope) return document.body;
    if (typeof scope === 'string') return document.querySelector(scope);
    return scope;
  }

  function scriptDir() {
    if (document.currentScript && document.currentScript.src) {
      return document.currentScript.src.replace(/[^/]+$/, '');
    }
    const s = document.querySelector('script[src*="simd-tooltips"]');
    if (s && s.src) return s.src.replace(/[^/]+$/, '');
    return './';
  }

  function joinUrl(base, name) {
    if (!base) return name;
    return base.replace(/\/?$/, '/') + name;
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function escapeAttr(s) { return escapeHtml(s); }

  // ---------------------------------------------------------------------
  // Styles (injected once)
  // ---------------------------------------------------------------------
  const STYLES = `
    .simd-intrinsic {
      cursor: help;
      border-bottom: 1px dashed currentColor;
    }
    /* SIMD types: visually distinct from intrinsics, and excluded from the
       Tab order (no tabindex set in the wrapper). Use a thinner dotted
       underline in an amber tone -- mirrors the type tag in the tooltip. */
    .simd-intrinsic.simd-type {
      cursor: help;
      border-bottom: 1px dotted #d09a3a;
    }
    /* Show the focus indicator on any focus -- click *and* keyboard tab --
       so the user can see which intrinsic is currently the active target. */
    .simd-intrinsic:focus {
      outline: 2px solid #4a7afe;
      outline-offset: 2px;
      border-radius: 2px;
      background: rgba(74,122,254,.08);
    }
    .simd-tooltip {
      position: absolute;
      z-index: 2147483647;
      display: none;
      max-width: 560px;
      padding: 10px 12px;
      background: #1f2430;
      color: #e6e8ee;
      border: 1px solid #3a3f4d;
      border-radius: 6px;
      box-shadow: 0 6px 24px rgba(0,0,0,.35);
      font: 13px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      pointer-events: auto;
    }
    /* Warmer tint for SIMD-type tooltips so the kind is recognizable at a glance. */
    .simd-tooltip.simd-tooltip--type {
      background: #2a2418;
      border-color: #5a4a2a;
    }
    .simd-tooltip.simd-tooltip--type .simd-tt-head code { color: #ffc977; }
    .simd-tooltip.simd-tooltip--type .simd-tt-typedef { background: #1a1610; color: #e8d8b8; }
    .simd-tooltip code, .simd-tooltip pre {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12.5px;
    }
    .simd-tt-head { display: flex; flex-wrap: wrap; gap: 6px; align-items: baseline; margin-bottom: 6px; }
    .simd-tt-head code { color: #ffd479; font-weight: 600; }
    .simd-tt-tag { font-size: 10.5px; padding: 1px 6px; background: #2c5282; color: #cfe1ff; border-radius: 999px; line-height: 1.5; }
    .simd-tt-arch { font-size: 10.5px; padding: 1px 6px; background: #553c5b; color: #ffd2f0; border-radius: 999px; line-height: 1.5; }
    .simd-tt-sig { margin: 0 0 6px 0; padding: 6px 8px; background: #131722; border-radius: 4px; white-space: pre-wrap; overflow-x: auto; }
    .simd-tt-typedef { display: block; margin: 0 0 6px 0; padding: 5px 8px; background: #131722; border-radius: 4px; color: #d8dde7; }
    .simd-tt-kind { font-size: 10.5px; padding: 1px 6px; background: #4a3c2a; color: #ffd58a; border-radius: 999px; line-height: 1.5; }
    .simd-tt-desc { color: #c8ccd6; }
    .simd-tt-foot { margin-top: 6px; }
    .simd-tt-foot a { color: #8fb6ff; text-decoration: none; }
    .simd-tt-foot a:hover { text-decoration: underline; }
    .simd-tt-loading { color: #888; }
    .simd-tt-error { color: #ff7b7b; }
    .simd-hint {
      position: absolute;
      display: none;
      z-index: 2147483647;
      font: 700 10px/1 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #4a7afe;
      color: #fff;
      padding: 2px 6px 3px;
      border-radius: 999px;
      cursor: pointer;
      user-select: none;
      box-shadow: 0 2px 6px rgba(0,0,0,.25);
      pointer-events: auto;
    }
    .simd-hint:hover { background: #2c5fff; }
    .simd-hint:focus-visible { outline: 2px solid #fff; outline-offset: 1px; }
  `;

  function injectStyles() {
    if (document.getElementById('simd-tooltip-styles')) return;
    const s = document.createElement('style');
    s.id = 'simd-tooltip-styles';
    s.textContent = STYLES;
    document.head.appendChild(s);
  }

  // ---------------------------------------------------------------------
  // Auto-init
  // ---------------------------------------------------------------------
  root.SimdTooltips = SimdTooltips;

  function readScriptOpts() {
    const s = document.currentScript || document.querySelector('script[src*="simd-tooltips"]');
    if (!s) return null;
    if (s.hasAttribute('data-no-auto')) return null;
    const opts = {};
    if (s.dataset.scope) opts.scope = s.dataset.scope;
    if (s.dataset.on) opts.on = s.dataset.on;
    if (s.hasAttribute('data-wrap')) opts.wrap = true;
    if (s.dataset.baseUrl) opts.baseUrl = s.dataset.baseUrl;
    if (s.dataset.namesUrl) opts.namesUrl = s.dataset.namesUrl;
    if (s.dataset.dataUrl) opts.dataUrl = s.dataset.dataUrl;
    return opts;
  }

  const auto = readScriptOpts();
  if (auto !== null) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => init(auto));
    } else {
      init(auto);
    }
  }
})(typeof window !== 'undefined' ? window : globalThis);
