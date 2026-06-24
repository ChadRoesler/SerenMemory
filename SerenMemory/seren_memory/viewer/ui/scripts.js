// ── SerenMemory viewer — leaf logic ──────────────────────────────────────────
// Snaps onto the SerenMeninges shell. The shell provides api() (same-origin,
// auto-attaches the saved bearer token), escapeHtml(), showTab(), getToken(),
// and the 🔑 token modal. This file CALLS them; it never redefines them.
//
// Memory is a SINGLE-CANVAS UI: one #entries list that switchTab() re-renders
// per Hall, plus the search bar (Search) and #overview-section (Overview). The
// old standalone viewer carried a cross-origin connection modal + a lock gate;
// served same-origin from /viewer those are gone - the shell's token modal and
// same-origin api() replace them. The embedder safe-mode MIGRATION controller
// (bottom of this file) is kept intact - it's load-bearing.

const state = { tab: 'short' };
const $ = (id) => document.getElementById(id);

// ----------------------------------------------------------------------
// Errors + time helpers
// ----------------------------------------------------------------------
function showError(msg) {
    $('error-slot').innerHTML = `<div class="err">⚠ ${escapeHtml(msg)}<br><span class="hint">Is SerenMemory reachable? If it has auth on, set the bearer token via 🔑 Token.</span></div>`;
}
function clearError() { $('error-slot').innerHTML = ''; }

function fmtTs(ts) {
    if (ts == null) return '-';
    const n = typeof ts === 'string' ? parseFloat(ts) : ts;
    if (!isFinite(n)) return String(ts);
    const d = new Date(n * 1000);
    if (isNaN(d.getTime())) return String(ts);
    const ageMs = Date.now() - d.getTime();
    return `${d.toLocaleString()} (${relAge(ageMs)})`;
}

function relAge(ms) {
    const s = Math.round(ms / 1000);
    if (s < 60) return `${s}s ago`;
    const m = Math.round(s / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.round(m / 60);
    if (h < 48) return `${h}h ago`;
    const d = Math.round(h / 24);
    return `${d}d ago`;
}

// ----------------------------------------------------------------------
// Rendering
// ----------------------------------------------------------------------
function renderEmbedder(model) {
    const pill = $('embedder-pill');
    if (!pill) return;
    pill.textContent = model || '';
    pill.title = model || '';
    pill.style.display = model ? '' : 'none';
}

function loadOverview(root) {
    const tiers = root.tiers || {};
    const stat = (label, n, cls) =>
        `<div class="stat ${cls}"><div class="big">${n ?? '?'}</div><div class="lbl">${label}</div></div>`;
    const statsHtml = [
        stat('short-term', tiers.short, 'tier-short'),
        stat('near-term',  tiers.near,  'tier-near'),
        stat('long-term',  tiers.long,  'tier-long'),
        stat('briefs',     tiers.briefs ?? '?', 'tier-brief'),
        stat('drafts',     tiers.drafts ?? '?', 'tier-draft'),
        tiers.pruned != null ? stat('pruned', tiers.pruned, '') : '',
    ].join('');

    const con = root.consolidator || {};
    const metaRows = [
        ['version',     root.version   || '?'],
        ['embedder',    root.embedding_model || '?'],
        ['consolidator',con.enabled ? `enabled · ${con.mode || '?'} · every ${con.interval_seconds ?? '?'}s` : 'disabled'],
    ].map(([k, v]) => `<div class="row"><span class="k">${k}</span><span class="v">${escapeHtml(String(v))}</span></div>`).join('');

    $('overview-section').innerHTML = `
        <div class="stat-row">${statsHtml}</div>
        <div class="overview-meta">${metaRows}</div>`;
}

function entryCard(tier, content, meta, opts = {}) {
    const badges = [];
    badges.push(`<span class="badge tier-${tier}">${tier}</span>`);
    if (opts.score != null) badges.push(`<span class="badge score">score ${opts.score.toFixed(3)}</span>`);
    if (meta.pinned === true || meta.pinned === 'true') badges.push(`<span class="badge pinned">pinned</span>`);
    if (meta.completed === true || meta.completed === 'true') badges.push(`<span class="badge completed">completed</span>`);
    if (meta.superseded_by) badges.push(`<span class="badge superseded">superseded</span>`);
    if (meta.forget_flag) badges.push(`<span class="badge superseded">flagged</span>`);
    if (meta.demoted_reason) badges.push(`<span class="badge superseded">demoted</span>`);

    const metaBits = [];
    if (meta.topic) metaBits.push(`<span><span class="k">topic</span> ${escapeHtml(meta.topic)}</span>`);
    if (meta.source) metaBits.push(`<span><span class="k">src</span> ${escapeHtml(meta.source)}</span>`);
    if (meta.evidence_count != null) metaBits.push(`<span><span class="k">evidence</span> ${escapeHtml(meta.evidence_count)}</span>`);
    const tsField = meta.ts ?? meta.created_at ?? meta.last_confirmed;
    if (tsField != null) metaBits.push(`<span><span class="k">when</span> ${escapeHtml(fmtTs(tsField))}</span>`);
    if (meta.trigger_type) {
        let trig = meta.trigger_type;
        if (meta.trigger_value) trig += ` -> ${meta.trigger_value}`;
        metaBits.push(`<span><span class="k">trigger</span> ${escapeHtml(trig)}</span>`);
    }
    if (meta.expires_at) metaBits.push(`<span><span class="k">expires</span> ${escapeHtml(fmtTs(meta.expires_at))}</span>`);
    if (opts.id) metaBits.push(`<code class="id">${escapeHtml(opts.id)}</code>`);

    return `
    <div class="entry ${tier}">
        <div class="content">${escapeHtml(content)}</div>
        <div class="meta">
            ${badges.join(' ')}
            ${metaBits.join('')}
        </div>
    </div>`;
}

function briefCard(brief) {
    const meta = brief.metadata || {};
    const id = brief.id;
    const promoteHints = Array.isArray(meta.promote_hints) ? meta.promote_hints : [];
    const noiseHints = Array.isArray(meta.noise_hints) ? meta.noise_hints : [];
    const intents = Array.isArray(meta.completed_intents) ? meta.completed_intents : [];
    const badges = [`<span class="badge tier-brief">brief</span>`];
    const metaBits = [];
    if (meta.created_at != null) metaBits.push(`<span><span class="k">when</span> ${escapeHtml(fmtTs(meta.created_at))}</span>`);
    metaBits.push(`<code class="id">${escapeHtml(id)}</code>`);
    const chipRows = [];
    if (promoteHints.length) chipRows.push(`<div class="chip-row"><span class="k">promote</span>` + promoteHints.map(h => `<span class="chip promote">${escapeHtml(h)}</span>`).join('') + `</div>`);
    if (noiseHints.length) chipRows.push(`<div class="chip-row"><span class="k">noise</span>` + noiseHints.map(h => `<span class="chip noise">${escapeHtml(h)}</span>`).join('') + `</div>`);
    if (intents.length) chipRows.push(`<div class="chip-row"><span class="k">completed</span>` + intents.map(i => `<span class="chip intent">${escapeHtml(i)}</span>`).join('') + `</div>`);
    return `
    <div class="entry brief">
        <div class="content">${escapeHtml(brief.content || '(no summary)')}</div>
        ${chipRows.join('')}
        <div class="meta">
            ${badges.join(' ')}
            ${metaBits.join('')}
        </div>
    </div>`;
}

function draftCard(draft) {
    const meta = draft.metadata || {};
    const id = draft.id;
    const status = meta.status || 'pending';
    const sourceShortIds = Array.isArray(meta.source_short_ids) ? meta.source_short_ids : [];
    const badges = [`<span class="badge tier-draft">draft</span>`];
    badges.push(`<span class="badge status-${escapeHtml(status)}">${escapeHtml(status)}</span>`);
    if (meta.long_term_id) badges.push(`<span class="badge">-> long</span>`);
    const metaBits = [];
    if (meta.topic) metaBits.push(`<span><span class="k">topic</span> ${escapeHtml(meta.topic)}</span>`);
    if (meta.evidence_count != null) metaBits.push(`<span><span class="k">evidence</span> ${escapeHtml(meta.evidence_count)}</span>`);
    if (meta.brief_id_used) metaBits.push(`<span><span class="k">via brief</span> <code class="id">${escapeHtml(meta.brief_id_used)}</code></span>`);
    if (meta.created_at != null) metaBits.push(`<span><span class="k">when</span> ${escapeHtml(fmtTs(meta.created_at))}</span>`);
    if (meta.reviewed_at != null) metaBits.push(`<span><span class="k">reviewed</span> ${escapeHtml(fmtTs(meta.reviewed_at))}</span>`);
    metaBits.push(`<code class="id">${escapeHtml(id)}</code>`);
    const chipRows = [];
    if (sourceShortIds.length) chipRows.push(`<div class="chip-row"><span class="k">evidence (${sourceShortIds.length} shorts)</span>` + sourceShortIds.map(sid => `<span class="chip evidence">${escapeHtml(String(sid).slice(0, 8))}…</span>`).join('') + `</div>`);
    let actionRow = '';
    if (status === 'pending') {
        actionRow = `
        <div class="actions">
            <button class="approve" onclick="approveDraft('${escapeHtml(id)}')">approve</button>
            <button class="reject" onclick="rejectDraft('${escapeHtml(id)}')">reject</button>
        </div>`;
    } else if (meta.review_note) {
        actionRow = `<div class="review-note">${escapeHtml(status)}: ${escapeHtml(meta.review_note)}</div>`;
    }
    return `
    <div class="entry draft">
        <div class="content">${escapeHtml(draft.content || '(empty synthesis)')}</div>
        ${chipRows.join('')}
        ${actionRow}
        <div class="meta">
            ${badges.join(' ')}
            ${metaBits.join('')}
        </div>
    </div>`;
}

async function approveDraft(draftId) {
    try {
        await api(`/drafts/${draftId}/approve`, { method: 'POST', body: '{}' });
        await loadDrafts();
    } catch (e) { showError(e.message); }
}

async function rejectDraft(draftId) {
    const reason = window.prompt('Reason for rejection? (required)');
    if (!reason || !reason.trim()) return;
    try {
        // app.py accepts {critique} (canonical) or legacy {reason}; send critique.
        await api(`/drafts/${draftId}/reject`, { method: 'POST', body: JSON.stringify({ critique: reason.trim() }) });
        await loadDrafts();
    } catch (e) { showError(e.message); }
}

function renderEntries(html) {
    $('entries').innerHTML = html || `<div class="empty">nothing here yet</div>`;
}

// ----------------------------------------------------------------------
// Loaders per tab
// ----------------------------------------------------------------------
async function loadShort() {
    setHint('Working memory · ~8 day lifetime · free read/write');
    setExtraToggle(null);
    const data = await api('/short?limit=200');
    renderEntries((data.entries || []).map(e => entryCard('short', e.content, e.metadata || {}, { id: e.id })).join(''));
}

async function loadNear() {
    setHint('Open loops · future-tense intents · lives until fulfilled or expired');
    setExtraToggle('include completed', async (checked) => loadNearInner(checked));
    await loadNearInner($('extra-toggle').checked);
}
async function loadNearInner(includeCompleted) {
    const data = await api(`/near?include_completed=${includeCompleted ? 'true' : 'false'}`);
    renderEntries((data.entries || []).map(e => entryCard('near', e.content, e.metadata || {}, { id: e.id })).join(''));
}

async function loadLong() {
    setHint('Consolidated knowledge · gated (consolidator writes only) · no scalpel');
    setExtraToggle('include superseded', async (checked) => loadLongInner(checked));
    await loadLongInner($('extra-toggle').checked);
}
async function loadLongInner(includeSuperseded) {
    const data = await api(`/long?include_superseded=${includeSuperseded ? 'true' : 'false'}`);
    renderEntries((data.entries || []).map(e => entryCard('long', e.content, e.metadata || {}, { id: e.id })).join(''));
}

async function loadBriefs() {
    setHint('Daily briefs · steering payload for the consolidator · newest first');
    setExtraToggle(null);
    const data = await api('/brief?limit=50');
    renderEntries((data.entries || []).map(b => briefCard(b)).join(''));
}

async function loadDrafts() {
    setHint('Consolidator drafts · synthesized candidates awaiting review · approve commits to LongTerm');
    setExtraToggle('include reviewed', async (checked) => loadDraftsInner(checked));
    await loadDraftsInner($('extra-toggle').checked);
}
async function loadDraftsInner(includeReviewed) {
    // Default view is pending-only (the actionable queue). Toggle drops the
    // status filter to surface approved + rejected history.
    const url = includeReviewed ? '/drafts?limit=50' : '/drafts?limit=50&status=pending';
    const data = await api(url);
    renderEntries((data.entries || []).map(d => draftCard(d)).join(''));
}

async function doSearch() {
    const query = $('search-query').value.trim();
    if (!query) { renderEntries(`<div class="empty">type a query above</div>`); return; }
    const body = {
        query,
        n_results: parseInt($('search-n').value || '10', 10),
        include_short: $('inc-short').checked,
        include_near: $('inc-near').checked,
        include_long: $('inc-long').checked,
    };
    try {
        clearError();
        const data = await api('/search', { method: 'POST', body: JSON.stringify(body) });
        const html = (data.hits || []).map(h => entryCard(h.tier, h.content, h.metadata || {}, { id: h.id, score: h.score })).join('');
        renderEntries(html || `<div class="empty">no hits for "${escapeHtml(query)}"</div>`);
    } catch (e) { showError(e.message); }
}

// ----------------------------------------------------------------------
// Toolbar helpers
// ----------------------------------------------------------------------
function setHint(text) { $('list-hint').textContent = text; }

let _extraHandler = null;
function setExtraToggle(label, handler) {
    const wrap = $('extra-toggle-wrap');
    if (!label) { wrap.style.display = 'none'; _extraHandler = null; return; }
    wrap.style.display = '';
    $('extra-label').textContent = label;
    $('extra-toggle').checked = false;
    _extraHandler = handler;
}
$('extra-toggle').addEventListener('change', (e) => { if (_extraHandler) _extraHandler(e.target.checked); });

// ----------------------------------------------------------------------
// Tab switching — single canvas. Uses the shell's showTab() for the active
// tab class (Memory has no .view panels, so showTab's panel toggle is a no-op),
// then shows/hides the search bar / entries / overview and lazy-loads the Hall.
// ----------------------------------------------------------------------
async function switchTab(tier) {
    state.tab = tier;
    showTab(tier);
    const isSearch = tier === 'search';
    const isOverview = tier === 'overview';
    $('searchbar').style.display = isSearch ? 'flex' : 'none';
    $('list-toolbar').style.display = (isSearch || isOverview) ? 'none' : 'flex';
    $('entries').style.display = isOverview ? 'none' : 'flex';
    $('overview-section').style.display = isOverview ? 'block' : 'none';
    clearError();
    try {
        if (tier === 'short') await loadShort();
        else if (tier === 'near') await loadNear();
        else if (tier === 'long') await loadLong();
        else if (tier === 'briefs') await loadBriefs();
        else if (tier === 'drafts') await loadDrafts();
        else if (tier === 'search') renderEntries(`<div class="empty">enter a query and hit search</div>`);
        else if (tier === 'overview') { const r = await api('/'); loadOverview(r); }
    } catch (e) { showError(e.message); renderEntries(''); }
}

$('do-search').addEventListener('click', doSearch);
$('search-query').addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(); });

// ----------------------------------------------------------------------
// Boot + reload. Probes / with a raw fetch (not the shell api()) so we can read
// the 503+{safe_mode} body the migration gate needs - the shell api() throws on
// a non-OK status. Auth header is built from the shell's saved token.
// ----------------------------------------------------------------------
async function boot() {
    clearError();
    let root = null;
    try {
        const h = { 'content-type': 'application/json' };
        const tok = getToken();
        if (tok) h['authorization'] = 'Bearer ' + tok;
        const resp = await fetch('/', { headers: h });
        root = await resp.json().catch(() => ({}));
        if (root && root.safe_mode) {
            if (window.checkMigrationGate && await window.checkMigrationGate()) return;
        }
        if (!resp.ok && !(root && root.safe_mode)) throw new Error(`HTTP ${resp.status}`);
    } catch (e) {
        showError(e.message);
        renderEmbedder(null);
        return;
    }
    renderEmbedder(root.embedding_model);
    // Double-check the gate (covers safe_mode clearing between / and here).
    if (window.checkMigrationGate && await window.checkMigrationGate()) return;
    loadOverview(root);
    await switchTab(state.tab);
}

// Header ⟳ button (header_aside.html) calls this.
function reload() { boot(); }

boot();

// ═══════════════════════════════════════════════════════════════════════
//  Migration modal controller. Reuses $, api, escapeHtml. Polls /migrate/status;
//  when safe_mode is true, raises the three-screen flow:
//  warning -> (deny: revert+restart) | (accept: migrate w/ bar -> done).
// ═══════════════════════════════════════════════════════════════════════
(function () {
    function ensureOverlay() {
        let o = $('mig-overlay');
        if (o) return o;
        o = document.createElement('div');
        o.id = 'mig-overlay';
        o.className = 'mig-overlay';
        o.innerHTML = `<div class="mig-card" id="mig-card"></div>`;
        document.body.appendChild(o);
        return o;
    }

    function show() { ensureOverlay().classList.add('show'); }
    function hide() { const o = $('mig-overlay'); if (o) o.classList.remove('show'); }
    function card() { return $('mig-card'); }

    function screenWarning(mm) {
        const from = (mm && mm.stamped_model) || 'all-MiniLM-L6-v2 (default)';
        const to = (mm && mm.configured_model) || 'all-MiniLM-L6-v2 (default)';
        card().innerHTML = `
            <h2><span class="warn-ico">&#9888;</span> Major Configuration change!</h2>
            <p><b>The embedder model has changed.</b> The memory store was built
            with one model; your config now asks for another. They produce
            incompatible vector spaces, so the data must be migrated before
            recall will work correctly.</p>
            <div class="models">
                <div class="row"><span class="k">From</span><span class="v from">${escapeHtml(from)}</span></div>
                <div class="row"><span class="k">To</span><span class="v to">${escapeHtml(to)}</span></div>
            </div>
            <p>This will <b>back up</b> the current store first (kept as a
            rollback), then re-embed every entry in place. It may take a while.
            Choose <b>Accept</b> to migrate, or <b>Deny</b> to revert the config
            to the previous model.</p>
            <div class="mig-actions">
                <button class="deny" id="mig-deny">Deny</button>
                <button class="accept" id="mig-accept">Accept</button>
            </div>`;
        $('mig-deny').onclick = onDeny;
        $('mig-accept').onclick = onAccept;
    }

    async function onDeny() {
        card().innerHTML = `<h2>Reverting configuration&#8230;</h2><p>Setting the embedder back to the previous model.</p>`;
        try {
            const r = await api('/migrate/deny', { method: 'POST', body: '{}' });
            card().innerHTML = `
                <h2>Config reverted</h2>
                <p>${escapeHtml(r.detail || 'The embedder model was reverted.')}</p>
                ${r.config_rewritten === false ? `<div class="mig-cmd">Set storage.embedding_model to ${escapeHtml(JSON.stringify(r.reverted_to))} yourself, then restart.</div>` : ''}
                <div class="mig-actions">
                    <button class="deny" id="mig-back">Back</button>
                    <button class="primary" id="mig-okay">Okay</button>
                </div>`;
            $('mig-back').onclick = () => screenWarning(window.__migMismatch);
            $('mig-okay').onclick = () => offerRestart('Config reverted. Restart to resume normally.');
        } catch (e) { screenError(e.message, () => screenWarning(window.__migMismatch)); }
    }

    async function onAccept() {
        card().innerHTML = `
            <h2>Migrating database&#8230;</h2>
            <div class="models">
                <div class="row"><span class="k">From</span><span class="v from" id="mig-from"></span></div>
                <div class="row"><span class="k">To</span><span class="v to" id="mig-to"></span></div>
            </div>
            <div class="mig-bar-wrap"><div class="mig-bar" id="mig-bar"></div></div>
            <div class="mig-pct" id="mig-pct">starting&#8230;</div>`;
        try { await api('/migrate/accept', { method: 'POST', body: '{}' }); pollMigration(); }
        catch (e) { screenError(e.message, () => screenWarning(window.__migMismatch)); }
    }

    function pollMigration() {
        const tick = async () => {
            try {
                const s = await api('/migrate/status');
                const m = s.migration || {};
                if ($('mig-from')) $('mig-from').textContent = m.from_model || '';
                if ($('mig-to')) $('mig-to').textContent = m.to_model || '';
                const pct = m.percent ?? 0;
                if ($('mig-bar')) $('mig-bar').style.width = pct + '%';
                if ($('mig-pct')) $('mig-pct').textContent = m.state === 'running' ? `${pct}% (${m.done}/${m.total})` : m.state;
                if (m.state === 'done') { screenDone(m); return; }
                if (m.state === 'error') { screenError(m.error || 'migration failed (store restored from backup)', () => screenWarning(window.__migMismatch)); return; }
                setTimeout(tick, 600);
            } catch (e) { screenError(e.message, () => screenWarning(window.__migMismatch)); }
        };
        tick();
    }

    function screenDone(m) {
        const stash = m.stash_dir ? `<p class="hint">Backup kept at: <code class="id">${escapeHtml(m.stash_dir)}</code></p>` : '';
        card().innerHTML = `
            <h2><span class="mig-done-ico">&#10003;</span> It&#39;s over!</h2>
            <p>Migration complete &#8212; ${escapeHtml(String(m.done ?? ''))} entries re-embedded into
            the new space. Restart SerenMemory to load the migrated store.</p>
            ${stash}
            <div class="mig-actions">
                <button class="primary" id="mig-restart">Restart service</button>
            </div>`;
        $('mig-restart').onclick = doRestart;
    }

    function offerRestart(msg) {
        card().innerHTML = `
            <h2>Ready to restart</h2>
            <p>${escapeHtml(msg)}</p>
            <div class="mig-actions">
                <button class="primary" id="mig-restart">Restart service</button>
            </div>`;
        $('mig-restart').onclick = doRestart;
    }

    async function doRestart() {
        card().innerHTML = `<h2>Restarting&#8230;</h2><p>Asking the service to restart.</p>`;
        try {
            const r = await api('/migrate/restart', { method: 'POST', body: '{}' });
            if (r.action === 'restarting') {
                card().innerHTML = `<h2>Restarting&#8230;</h2><p>The service is restarting. This page will reconnect in a few seconds.</p>`;
                setTimeout(() => { window.location.reload(); }, 4000);
            } else {
                card().innerHTML = `
                    <h2>Manual restart needed</h2>
                    <p>${escapeHtml(r.detail || 'Restart SerenMemory to load the migrated store.')}</p>
                    ${r.hint ? `<div class="mig-cmd">${escapeHtml(r.hint)}</div>` : ''}
                    <div class="mig-actions">
                        <button class="primary" id="mig-reload">I&#39;ve restarted &#8212; reload</button>
                    </div>`;
                $('mig-reload').onclick = () => window.location.reload();
            }
        } catch (e) {
            card().innerHTML = `<h2>Restarting&#8230;</h2><p>Connection closed (the service may be restarting). Reload to reconnect.</p><div class="mig-actions"><button class="primary" id="mig-reload">Reload</button></div>`;
            $('mig-reload').onclick = () => window.location.reload();
        }
    }

    function screenError(msg, onBack) {
        card().innerHTML = `
            <h2>Something went wrong</h2>
            <div class="mig-err">${escapeHtml(msg)}</div>
            <div class="mig-actions"><button class="deny" id="mig-err-back">Back</button></div>`;
        $('mig-err-back').onclick = onBack || hide;
    }

    window.checkMigrationGate = async function () {
        try {
            const s = await api('/migrate/status');
            if (s && s.safe_mode) {
                window.__migMismatch = s.mismatch || {};
                ensureOverlay();   // mig-card must be in the DOM before screenWarning writes to it
                show();
                screenWarning(window.__migMismatch);
                return true;
            }
            hide();
            return false;
        } catch (e) { hide(); return false; }
    };
})();
