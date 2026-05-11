/**
 * simd-tooltip VS Code extension.
 *
 * Registers a HoverProvider that returns a Markdown card for every SIMD
 * intrinsic (or SIMD type) the cursor lands on. Backed by the same
 * `simd-data.json` the JS library uses, vendored under ./data/.
 *
 * The data file is loaded lazily on first hover so VS Code startup isn't
 * affected. After that the records map sits in memory for the session.
 */

const vscode = require('vscode');
const fs = require('fs');
const path = require('path');

// Word pattern — must match the C-identifier shape we tokenize against
// elsewhere in the project. VS Code's default word pattern excludes
// underscores in some configurations, so we pass our own to be sure.
const WORD_PATTERN = /[A-Za-z_][A-Za-z0-9_]*/;

let recordsCache = null;
let clustersCache = null;
let dataPromise = null;
let dataMissingWarned = false;

function ensureRecords() {
    if (recordsCache) return Promise.resolve(recordsCache);
    if (dataPromise) return dataPromise;

    const dataPath = path.join(__dirname, 'data', 'simd-data.json');
    dataPromise = new Promise((resolve, reject) => {
        fs.readFile(dataPath, 'utf8', (err, txt) => {
            if (err) return reject(err);
            try {
                const doc = JSON.parse(txt);
                recordsCache = doc.records || doc;
                clustersCache = doc.clusters || {};
                resolve(recordsCache);
            } catch (e) { reject(e); }
        });
    }).catch(err => {
        if (!dataMissingWarned) {
            dataMissingWarned = true;
            vscode.window.showWarningMessage(
                `simd-tooltip: failed to load ${path.relative(__dirname, dataPath)}: ${err.message}. ` +
                `Did you run sync.sh after building the database?`
            );
        }
        recordsCache = {};
        return recordsCache;
    });
    return dataPromise;
}

function activate(context) {
    const provider = {
        provideHover(document, position) {
            const range = document.getWordRangeAtPosition(position, WORD_PATTERN);
            if (!range) return null;
            const word = document.getText(range);
            // Cheap filter: SIMD names always contain '_' and are at least 4 chars.
            if (word.length < 4 || word.indexOf('_') < 0) return null;
            return ensureRecords().then(records => {
                const rec = records[word];
                if (!rec) return null;
                const config = vscode.workspace.getConfiguration('simdVscode');
                return new vscode.Hover(formatHover(word, rec, config), range);
            });
        }
    };

    const config = vscode.workspace.getConfiguration('simdVscode');
    const languages = config.get('languages') || ['c', 'cpp'];
    for (const lang of languages) {
        context.subscriptions.push(
            vscode.languages.registerHoverProvider(lang, provider)
        );
    }
}

function deactivate() {
    recordsCache = null;
    dataPromise = null;
}

// ---------------------------------------------------------------------------
// Markdown rendering
// ---------------------------------------------------------------------------
function formatHover(name, rec, config) {
    const md = new vscode.MarkdownString();
    md.isTrusted = true;
    md.supportHtml = false;

    // Header line: name + kind + family/arch tags.
    const headerBits = [`**\`${name}\`**`];
    if (rec.kind === 'type') headerBits.push('_(SIMD type)_');
    md.appendMarkdown(headerBits.join(' ') + '\n\n');

    const tags = [];
    for (const f of rec.family || []) tags.push(`\`${f}\``);
    for (const a of rec.arch || [])   tags.push(`\`${a}\``);
    if (tags.length) md.appendMarkdown(tags.join(' · ') + '\n\n');

    // Signature / typedef.
    if (rec.definition) md.appendCodeblock(rec.definition, 'c');

    // Description.
    if (rec.description) md.appendMarkdown('\n' + rec.description + '\n');

    // Pseudocode (toggle via setting).
    const pcMode = (config && config.get && config.get('pseudocode')) || 'expanded';
    if (rec.pseudocode && pcMode !== 'off') {
        md.appendMarkdown('\n**pseudocode:**\n');
        md.appendCodeblock(rec.pseudocode);
    }

    // Variants: other intrinsics with the same upstream pseudocode.
    if (rec.cluster && clustersCache && clustersCache[rec.cluster]) {
        const siblings = clustersCache[rec.cluster].filter(n => n !== name);
        if (siblings.length > 0) {
            const MAX = 8;
            const shown = siblings.slice(0, MAX).map(n => '`' + n + '`').join(', ');
            const more = siblings.length > MAX ? ` _+${siblings.length - MAX} more_` : '';
            md.appendMarkdown(`\n**variants (${siblings.length}):** ${shown}${more}\n`);
        }
    }

    // Worked example (verified by simd-scribe -- a real round-tripped
    // input/output pair).
    if (rec.example) {
        md.appendMarkdown('\n**example:**\n');
        md.appendCodeblock(formatExample(rec.example), 'text');
    }

    // Footer links: upstream docs + Compiler Explorer + simd.dev page.
    const links = [];
    if (rec.doc_url) {
        const label = rec.source === 'arm-acle' ? 'Arm developer docs' : 'Intel Intrinsics Guide';
        links.push(`[${label} →](${rec.doc_url})`);
    }
    const ceUrl = compilerExplorerUrl(rec);
    if (ceUrl) {
        links.push(`[Compiler Explorer →](${ceUrl})`);
    }
    if (rec.example) {
        const safe = encodeURIComponent(name);
        links.push(`[Editable example on simd.dev →](https://simd.dev/?intrinsic=${safe})`);
    }
    if (links.length) md.appendMarkdown('\n' + links.join(' · ') + '\n');

    // Attribution footer: link the intrinsic name to its dedicated
    // page, and "simd.dev" to the database home. Both are friendlier
    // landing pages than the GitHub repo.
    const safe = encodeURIComponent(name);
    md.appendMarkdown(
        `\n\n---\n\n_[${name}](https://simd.dev/?intrinsic=${safe}) ` +
        'on [simd.dev](https://simd.dev/)_'
    );

    return md;
}

// ---------------------------------------------------------------------------
// Compiler Explorer URL builder
//
// Same logic as simd-tooltips.js (web library) -- duplicated here because the
// extension can't pull in that file at runtime. If we ever factor it into a
// shared module, both surfaces would call the same code.
// ---------------------------------------------------------------------------

const CE_INTEL_FLAGS = {
    'MMX': '-mmmx', 'SSE': '-msse', 'SSE2': '-msse2', 'SSE3': '-msse3',
    'SSSE3': '-mssse3', 'SSE4.1': '-msse4.1', 'SSE4.2': '-msse4.2',
    'AVX': '-mavx', 'AVX2': '-mavx2',
    'FMA': '-mfma', 'AES': '-maes', 'SHA': '-msha', 'SHA512': '-msha512',
    'BMI1': '-mbmi', 'BMI2': '-mbmi2', 'POPCNT': '-mpopcnt',
    'F16C': '-mf16c', 'GFNI': '-mgfni', 'VAES': '-mvaes',
    'VPCLMULQDQ': '-mvpclmulqdq', 'PCLMULQDQ': '-mpclmul',
    'AVX512F': '-mavx512f', 'AVX512VL': '-mavx512vl',
    'AVX512BW': '-mavx512bw', 'AVX512DQ': '-mavx512dq',
    'AVX512CD': '-mavx512cd', 'AVX512_BF16': '-mavx512bf16',
    'AVX512_FP16': '-mavx512fp16', 'AVX512_VBMI': '-mavx512vbmi',
    'AVX512_VBMI2': '-mavx512vbmi2', 'AVX512_VNNI': '-mavx512vnni',
    'AVX512_BITALG': '-mavx512bitalg', 'AVX512VPOPCNTDQ': '-mavx512vpopcntdq',
    'AVX512IFMA52': '-mavx512ifma', 'AVX512_VP2INTERSECT': '-mavx512vp2intersect',
    'AVX_VNNI': '-mavxvnni', 'AVX_VNNI_INT8': '-mavxvnniint8',
    'AVX_VNNI_INT16': '-mavxvnniint16', 'AVX_IFMA': '-mavxifma',
    'AVX_NE_CONVERT': '-mavxneconvert',
};

// See the same struct in simd-tooltip/dist/simd-tooltips.js for the full
// rationale on the headers, marches, and the +fp16+bf16+... extension list.
const ARM_EXT = '+fp16+bf16+i8mm+dotprod+crypto';
const CE_ARM_ARCHS = {
    'Neon':         { compiler: 'armv8-full-cclang-trunk', march: 'armv8.6-a' + ARM_EXT,                                       headers: ['arm_neon.h', 'arm_fp16.h', 'arm_bf16.h'] },
    'SVE':          { compiler: 'armv8-full-cclang-trunk', march: 'armv8.6-a+sve' + ARM_EXT,                                   headers: ['arm_sve.h', 'arm_neon_sve_bridge.h'] },
    'SVE2':         { compiler: 'armv8-full-cclang-trunk', march: 'armv9-a' + ARM_EXT,                                         headers: ['arm_sve.h', 'arm_neon_sve_bridge.h'] },
    'SME and SME2': { compiler: 'armv8-full-cclang-trunk', march: 'armv9.2-a+sme2+sme-i16i64+sme-f64f64' + ARM_EXT,             headers: ['arm_sve.h', 'arm_sme.h', 'arm_neon_sve_bridge.h'] },
    'Helium':       { compiler: 'armv7-cclang-trunk',      march: 'armv8.1-m.main+mve.fp+fp.dp',                                headers: ['arm_mve.h', 'arm_fp16.h', 'arm_bf16.h'] },
};
const CE_ARM_ARCH_ORDER = ['Neon', 'Helium', 'SVE', 'SVE2', 'SME and SME2'];

function ceParseSignature(def) {
    if (!def) return null;
    const flat = def.replace(/\s+/g, ' ').trim();
    const open = flat.indexOf('(');
    const close = flat.lastIndexOf(')');
    if (open < 0 || close < 0 || close < open) return null;
    const head = flat.slice(0, open).trim();
    const paramStr = flat.slice(open + 1, close).trim();
    const headParts = head.split(/\s+/);
    const name = headParts.pop();
    const returnType = headParts.join(' ');
    if (!paramStr || paramStr === 'void') {
        return { returnType, name, params: 'void', argList: '' };
    }
    const params = paramStr.split(',').map(p => p.trim());
    const argNames = params.map(p => {
        if (/\bconst\s+int\b/.test(p) && !/\*/.test(p)) return '0';
        const m = p.match(/([A-Za-z_]\w*)\s*$/);
        return m ? m[1] : '0';
    });
    return { returnType, name, params: paramStr, argList: argNames.join(', ') };
}

function ceConfigFor(rec) {
    if (!rec || rec.kind === 'type') return null;
    if (rec.source === 'arm-acle') {
        const fset = new Set(rec.family || []);
        for (const archKey of CE_ARM_ARCH_ORDER) {
            if (fset.has(archKey)) {
                const a = CE_ARM_ARCHS[archKey];
                return {
                    compiler: a.compiler,
                    options: `-O2 -march=${a.march}`,
                    headers: a.headers,
                };
            }
        }
        return null;
    }
    if (rec.source === 'intel-iguide') {
        const flags = [];
        for (const f of rec.family || []) {
            const flag = CE_INTEL_FLAGS[f];
            if (flag && flags.indexOf(flag) < 0) flags.push(flag);
        }
        if (flags.length === 0) flags.push('-mavx2');
        return {
            compiler: 'cclang_trunk',
            options: '-O2 ' + flags.join(' '),
            headers: ['immintrin.h'],
        };
    }
    return null;
}

function compilerExplorerUrl(rec) {
    const cfg = ceConfigFor(rec);
    if (!cfg) return null;
    const sig = ceParseSignature(rec.definition);
    if (!sig) return null;

    const includes = cfg.headers.map(h => `#include <${h}>`).join('\n');
    const source =
        `${includes}\n\n` +
        `${sig.returnType} example(${sig.params}) {\n` +
        `    return ${sig.name}(${sig.argList});\n` +
        `}\n`;

    const state = {
        sessions: [{
            id: 1,
            language: 'c',
            source: source,
            compilers: [{
                id: cfg.compiler,
                options: cfg.options,
                libs: [],
                filters: {
                    binary: false, commentOnly: true, demangle: true, directives: true,
                    execute: false, intel: true, labels: true, libraryCode: false, trim: true,
                },
            }],
        }],
        version: 4,
    };
    const utf8 = Buffer.from(JSON.stringify(state), 'utf8').toString('base64');
    const safe = utf8.replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    return 'https://godbolt.org/clientstate/' + safe;
}

// ---------------------------------------------------------------------------
// Example formatter -- inputs + output, column-aligned for the hover.
// ---------------------------------------------------------------------------
function formatExample(ex) {
    const inputs = ex.inputs || [];
    const out = ex.output || {};
    const outVals = Array.isArray(out.values) ? out.values : [out.values];

    const rows = [];
    for (const inp of inputs) {
        const isPtr = /\*\s*$/.test(inp.type || '');
        rows.push({
            label: (isPtr ? '*' : '') + (inp.name || '') + ':',
            values: Array.isArray(inp.values) ? inp.values : [inp.values],
        });
    }
    rows.push({ label: '→', values: outVals });

    const lanes = Math.max(1, ...rows.map(r => r.values.length));
    const colWidths = new Array(lanes).fill(0);
    for (const r of rows) {
        for (let i = 0; i < r.values.length; i++) {
            const w = String(r.values[i]).length;
            if (w > colWidths[i]) colWidths[i] = w;
        }
    }
    const labelWidth = Math.max(...rows.map(r => r.label.length));

    return rows.map(r => {
        const cells = r.values.map((v, i) =>
            String(v).padStart(colWidths[i], ' ')
        ).join(' ');
        return r.label.padEnd(labelWidth, ' ') + ' ' + cells;
    }).join('\n');
}

module.exports = { activate, deactivate };
