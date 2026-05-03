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

    // Upstream docs link.
    if (rec.doc_url) {
        const label = rec.source === 'arm-acle'
            ? 'Arm developer docs'
            : 'Intel Intrinsics Guide';
        md.appendMarkdown(`\n[${label} →](${rec.doc_url})\n`);
    }

    // Attribution footer.
    md.appendMarkdown(
        '\n\n---\n\n_[simd-vscode](https://github.com/MarcinZukowski/simd.dev) ' +
        'by [simd.dev](https://github.com/MarcinZukowski/simd.dev)_'
    );

    return md;
}

module.exports = { activate, deactivate };
