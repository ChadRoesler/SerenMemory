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
    const b = document.getElementById("embedderBadge");
    if (!b) return;
    b.textContent = "embedder: " + (model || '');
    b.className = "head-pill" + (model === '' ? "" : model === "all-MiniLM-L6-v2" ? "" : " hot");
}

function loadOverview(root) {
    const tiers = root.tiers || {};
    const stat = (label, n, cls) =>
        `<div class="stat ${cls}"><div class="big">${n ?? '?'}</div><div class="lbl">${label}</div></div>`;
    const eid = root.eidetic || null;
    const statsHtml = [
        stat('short-term', tiers.short, 'tier-short'),
        stat('near-term',  tiers.near,  'tier-near'),
        stat('long-term',  tiers.long,  'tier-long'),
        stat('briefs',     tiers.briefs ?? '?', 'tier-brief'),
        stat('drafts',     tiers.drafts ?? '?', 'tier-draft'),
        tiers.pruned != null ? stat('pruned', tiers.pruned, '') : '',
        eid ? stat('eidetic pictures', `${eid.count}<span class="sub">${human(eid.bytes)}</span>`, 'tier-eidetic') : '',
    ].join('');

    const metaRows = [
        ['version',     root.version   || '?'],
        ['embedder',    root.embedding_model || '?'],
        ['consolidation', root.consolidation || 'seren-hippocampus'],
    ].map(([k, v]) => `<div class="row"><span class="k">${k}</span><span class="v">${escapeHtml(String(v))}</span></div>`).join('');

    $('overview-section').innerHTML = `
        <div class="stat-row">${statsHtml}</div>
        <div class="overview-meta">${metaRows}</div>
        <h3 class="group-h">Backups</h3>
        <div class="backups" id="backups"><div class="empty">Loading…</div></div>`;
    loadBackups();
}

// ----------------------------------------------------------------------
// Eidetic: the pictures kept with an entry (an eidetic memory is one you can
// still see). A picture can't ride in an <img src>: the browser sends no
// bearer with it, so each thumbnail is fetched through the token (apiBlob)
// and shown from an object URL. Still pictures are shown; a clip, a sound or
// a PDF is a chip saying what it is and how big. 15 Oct 2026.
// ----------------------------------------------------------------------
const STILL = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
async function apiBlob(path) {
    const headers = {};
    const tok = getToken();
    if (tok) headers['Authorization'] = 'Bearer ' + tok;
    const res = await fetch(path, { headers });
    if (!res.ok) throw new Error(res.status + ' ' + res.statusText);
    return URL.createObjectURL(await res.blob());
}
function human(n) {
    n = Number(n || 0);
    for (const u of ['B', 'KB', 'MB', 'GB']) { if (n < 1024 || u === 'GB') return (u === 'B' ? n : n.toFixed(1)) + ' ' + u; n /= 1024; }
}
function eideticStrip(id, meta) {
    let rows = meta.eidetic;
    if (typeof rows === 'string') { try { rows = JSON.parse(rows); } catch (_) { rows = []; } }
    if (!Array.isArray(rows) || !rows.length || !id) return '';
    return `<div class="figs">` + rows.map(k => {
        const title = escapeHtml(k.caption || k.file || '');
        if (STILL.has(k.mime)) {
            return `<figure class="fig" title="${title}"><img alt="${title}" data-fig="/memory/${encodeURIComponent(id)}/eidetic/${encodeURIComponent(k.digest)}"><figcaption>${title}</figcaption></figure>`;
        }
        return `<span class="fig chip" title="${title}">${escapeHtml((k.mime || 'file').split('/')[0])} · ${human(k.bytes)}</span>`;
    }).join('') + `</div>`;
}
function hydrateFigures(root) {
    // img, video and audio take the object URL as src; a link (a PDF) as href.
    root.querySelectorAll('[data-fig]').forEach(async el => {
        const path = el.dataset.fig;
        el.removeAttribute('data-fig');
        try {
            const url = await apiBlob(path);
            if (el.tagName === 'A') el.href = url; else el.src = url;
            el.classList.add('ready');
        }
        catch (e) { el.replaceWith(Object.assign(document.createElement('span'), { className: 'fig chip', textContent: 'picture missing' })); }
    });
}

// ----------------------------------------------------------------------
// Backups: the same panel on every Seren viewer. Reads /stores
// (seren_sinew.stores): what is kept, the snapshots, when a Lodestar last
// pulled, how the last rehearsal went. Counts and times only. Kept parallel
// with Loci's and Margin's copy on purpose: fix one, fix all.
// ----------------------------------------------------------------------
function renderBackups(el, d) {
    if (!d || !d.ok) { el.innerHTML = `<div class="empty">this service keeps no snapshots (backup.enabled is off)</div>`; return; }
    const s = d.snapshots || {};
    const ago = (ts) => relAge(Date.now() - ts * 1000);
    const latest = s.latest ? `${escapeHtml(s.latest.id)} · ${ago(s.latest.created_at)} · ${escapeHtml(s.latest.reason || '')}` : 'none yet';
    const pull = s.last_pull ? `${ago(s.last_pull.created_at)} (${escapeHtml(s.last_pull.id)})` : 'never from this box';
    const r = d.last_rehearsal;
    const reh = r ? `<span class="${r.ok ? 'ok' : 'bad'}">${r.ok ? 'ok' : 'FAILED'}</span> · ${ago(r.at)} · ${escapeHtml(r.snapshot)} (${escapeHtml(r.source)}${r.problems ? `, ${r.problems} problem${r.problems === 1 ? '' : 's'}` : ''})`
                  : 'none since this process started';
    const stores = (d.stores || []).map(st =>
        `<div class="row"><span class="k">${escapeHtml(st.name)}</span><span class="v">${escapeHtml(st.kind)} · ${human(st.bytes)}${st.backed_up ? '' : ' · not copied'}${st.exists ? '' : ' · nothing on disk yet'}</span></div>`).join('');
    el.innerHTML = `
      <div class="row"><span class="k">snapshots</span><span class="v">${s.count ?? 0} kept · newest ${s.keep_daily} daily, then one a week for ${s.keep_weekly} weeks</span></div>
      <div class="row"><span class="k">newest</span><span class="v">${latest}</span></div>
      <div class="row"><span class="k">last pull</span><span class="v">${pull}</span></div>
      <div class="row"><span class="k">last rehearsal</span><span class="v">${reh}</span></div>
      ${stores}`;
}
async function loadBackups() {
    const el = $('backups');
    if (!el) return;
    try { renderBackups(el, await api('/stores')); }
    catch (e) { el.innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`; }
}

function entryCard(tier, content, meta, opts = {}) {
    const badges = [];
    badges.push(`<span class="badge tier-${tier}">${tier}</span>`);
    if (meta.kind === 'satellite') badges.push(`<span class="badge satellite">satellite</span>`);
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
        ${eideticStrip(opts.id, meta)}
        <div class="meta">
            ${badges.join(' ')}
            ${metaBits.join('')}
        </div>
        ${opts.under || ''}
    </div>`;
}

// A core's surroundings, under the core: the satellites (the dated episodes
// that stand behind it) and, when it was reworded, what it said before. They
// were in the store all along and nowhere in this window; now a long-term row
// expands and shows its satellites under it.
function satelliteRow(s) {
    const m = s.metadata || {};
    const when = m.created_at ?? m.last_confirmed;
    const bits = [`<span class="badge satellite">satellite</span>`];
    if (when != null) bits.push(`<span><span class="k">when</span> ${escapeHtml(fmtTs(when))}</span>`);
    if (m.evidence_count != null) bits.push(`<span><span class="k">evidence</span> ${escapeHtml(m.evidence_count)}</span>`);
    bits.push(`<code class="id">${escapeHtml(s.id)}</code>`);
    return `<div class="sat"><div class="content">${escapeHtml(s.content)}</div>${eideticStrip(s.id, m)}<div class="meta">${bits.join(' ')}</div></div>`;
}

function earlierWording(meta) {
    // restated_from equal to the content means a restate was undone: nothing earlier to show.
    const was = meta.restate_undone || '';
    if (!was) return '';
    return `<div class="sat was"><div class="content">${escapeHtml(was)}</div><div class="meta">
        <span class="badge superseded">reworded, then put back</span>
        ${meta.restate_undone_reason ? `<span><span class="k">why</span> ${escapeHtml(meta.restate_undone_reason)}</span>` : ''}</div></div>`;
}

function surroundingsBlock(coreId, meta, sats, lazyCount) {
    // sats given = render them; lazyCount given = fetch on first open (search hits)
    const n = sats ? sats.length : (lazyCount || 0);
    const earlier = earlierWording(meta);
    if (!n && !earlier) return '';
    const label = `${n} satellite${n === 1 ? '' : 's'}${earlier ? ' · an earlier wording' : ''}`;
    const body = sats ? sats.map(satelliteRow).join('') + earlier
                      : `<div class="sat-slot" data-core="${escapeHtml(coreId)}"><div class="empty">loading…</div></div>${earlier}`;
    return `<details class="sats"${sats ? '' : ' data-lazy="1"'}><summary>${label}</summary>${body}</details>`;
}

// search hits: the satellites are fetched the first time a core is opened
document.addEventListener('toggle', async (ev) => {
    const d = ev.target;
    if (!d.matches || !d.matches('details.sats[data-lazy]') || !d.open) return;
    d.removeAttribute('data-lazy');
    const slot = d.querySelector('.sat-slot');
    try {
        const data = await api(`/long/${encodeURIComponent(slot.dataset.core)}/satellites`);
        slot.innerHTML = (data.satellites || []).map(satelliteRow).join('') || `<div class="empty">none</div>`;
        hydrateFigures(slot);
    } catch (e) { slot.innerHTML = `<div class="empty">could not load: ${escapeHtml(e.message)}</div>`; }
}, true);

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
    // A hippocampus draft: a list of operations on long-term, each with its
    // own verdict. Read-only here - the main model reviews, over MCP or
    // POST /drafts/{id}/review; this is the window onto the queue.
    const id = draft.id;
    const status = draft.status || 'pending';
    const badges = [`<span class="badge tier-draft">draft</span>`,
                    `<span class="badge status-${escapeHtml(status)}">${escapeHtml(status)}</span>`];
    if (draft.attempt > 1 || draft.terminal) badges.push(`<span class="badge">attempt ${escapeHtml(draft.attempt)}${draft.terminal ? ' · terminal' : ''}</span>`);
    const metaBits = [];
    if (draft.brief_id_used) metaBits.push(`<span><span class="k">via brief</span> <code class="id">${escapeHtml(draft.brief_id_used)}</code></span>`);
    if (draft.created_at != null) metaBits.push(`<span><span class="k">when</span> ${escapeHtml(fmtTs(draft.created_at))}</span>`);
    if (draft.reviewed_at != null) metaBits.push(`<span><span class="k">reviewed</span> ${escapeHtml(fmtTs(draft.reviewed_at))}</span>`);
    metaBits.push(`<code class="id">${escapeHtml(id)}</code>`);
    const ops = (draft.operations || []).map(op => {
        const bits = [`<span class="badge">${escapeHtml(op.kind)}</span>`,
                      `<span class="badge status-${escapeHtml(op.status || 'pending')}">${escapeHtml(op.status || 'pending')}</span>`];
        if (op.topic) bits.push(`<span><span class="k">topic</span> ${escapeHtml(op.topic)}</span>`);
        if (op.target_core_id) bits.push(`<span><span class="k">core</span> <code class="id">${escapeHtml(String(op.target_core_id).slice(0, 8))}…</code></span>`);
        else if (op.target_op != null) bits.push(`<span><span class="k">core</span> op ${escapeHtml(String(op.target_op))}'s new core</span>`);
        const shorts = op.source_short_ids || [];
        if (shorts.length) bits.push(`<span><span class="k">evidence</span> ${shorts.length} short${shorts.length === 1 ? '' : 's'}</span>`);
        const said = op.edited_content || op.content || '(empty)';
        const why = op.critique ? `<div class="review-note">denied: ${escapeHtml(op.critique)}</div>`
                  : op.rationale ? `<div class="review-note">why: ${escapeHtml(op.rationale)}</div>` : '';
        return `<div class="op"><div class="content">${escapeHtml(said)}</div>${why}<div class="meta">${bits.join(' ')}</div></div>`;
    }).join('');
    return `
    <div class="entry draft">
        <div class="content">${escapeHtml(draft.summary || '(no summary)')}</div>
        ${ops}
        <div class="meta">
            ${badges.join(' ')}
            ${metaBits.join('')}
        </div>
    </div>`;
}

function renderEntries(html) {
    $('entries').innerHTML = html || `<div class="empty">nothing here yet</div>`;
    hydrateFigures($('entries'));
}

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
    // One call brings the satellites along; they are grouped under their cores here.
    const data = await api(`/long?include_satellites=true&include_superseded=${includeSuperseded ? 'true' : 'false'}`);
    const rows = data.entries || [];
    const byCore = {};
    for (const e of rows) {
        const m = e.metadata || {};
        if (m.kind === 'satellite' && m.core_id) (byCore[m.core_id] = byCore[m.core_id] || []).push(e);
    }
    for (const k in byCore) byCore[k].sort((a, b) => ((a.metadata || {}).created_at || 0) - ((b.metadata || {}).created_at || 0));
    const cores = rows.filter(e => (e.metadata || {}).kind !== 'satellite');
    const withSats = cores.filter(e => byCore[e.id]).length;
    setHint(`Consolidated knowledge · gated (consolidator writes only) · no scalpel · ${cores.length} cores, ` +
            `${withSats} with satellites: open one to see what stands behind it`);
    renderEntries(cores.map(e => entryCard('long', e.content, e.metadata || {},
        { id: e.id, under: surroundingsBlock(e.id, e.metadata || {}, byCore[e.id] || [], 0) })).join(''));
}

// ----------------------------------------------------------------------
// The Eidetic Hall: picture first. Every picture once, the newest kept first;
// open one and it shows what it is attached to. A picture rides on more than
// one memory by design (a core inherits its short-terms' pictures, an attach
// gives the core its satellite's, a supersession carries them forward), so the
// gallery is keyed by the picture and "what is this" is a list. 19 Oct
// 2026: "a gallery of images, then when clicked shows what its attached to ...
// eidetic is picture first".
// ----------------------------------------------------------------------
let _gallery = [];
function picPath(p) {
    return `/memory/${encodeURIComponent(p.via)}/eidetic/${encodeURIComponent(p.digest)}`;
}
function picKind(p) { return (p.mime || 'file').split('/')[0]; }
// what a tile says when it is not a still: eidetic keeps sounds and clips too
// (my voice lines from 17 Oct are six of the first thirteen)
function picGlyph(p) {
    const k = picKind(p);
    return k === 'audio' ? '♪ sound' : k === 'video' ? '▶ clip' : p.mime === 'application/pdf' ? 'PDF' : k;
}

async function loadEidetic() {
    setExtraToggle('include the archive', async (checked) => loadEideticInner(checked));
    await loadEideticInner($('extra-toggle').checked);
}
async function loadEideticInner(includePruned) {
    setHint('Pictures kept with memories · newest first · open one to see what it is attached to');
    const data = await api(`/eidetic?include_pruned=${includePruned ? 'true' : 'false'}&limit=1000`);
    _gallery = data.pictures || [];
    if (!_gallery.length) {
        renderEntries(`<div class="empty">${data.enabled === false ? 'this store keeps no pictures (eidetic.enabled is off)' : 'no pictures kept yet'}</div>`);
        return;
    }
    setHint(`${data.count} picture${data.count === 1 ? '' : 's'} · ${human(data.bytes)} · newest first · open one to see what it is attached to`);
    renderEntries(`<div class="gallery">${_gallery.map(galleryTile).join('')}</div>`);
}
function galleryTile(p, i) {
    const n = (p.entries || []).length;
    const title = escapeHtml(p.caption || p.file || p.digest.slice(0, 12));
    const on = `<span class="on">on ${n} memor${n === 1 ? 'y' : 'ies'}</span>`;
    const face = p.missing ? `<span class="what">picture missing</span>`
               : STILL.has(p.mime) ? `<img alt="${title}" data-fig="${picPath(p)}">`
               : `<span class="what"><span class="glyph">${escapeHtml(picGlyph(p))}</span><br>${human(p.bytes)}</span>`;
    return `<button class="tile${p.missing ? ' missing' : ''}" data-pic="${i}" title="${title}">${face}${on}</button>`;
}
$('entries').addEventListener('click', (ev) => {
    const t = ev.target.closest('.tile[data-pic]');
    if (t) openPicture(parseInt(t.dataset.pic, 10));
});

function attachedRow(e, shown) {
    // A satellite brings the core it hangs under - in full, unless that core keeps
    // the picture too and is already in this list (an attach gives the core its
    // satellite's pictures, so it usually is).
    const m = e.metadata || {};
    let under = '';
    if (e.core && shown.has(e.core.id)) {
        under = `<div class="sat core-of"><div class="meta"><span class="badge tier-long">hangs under the core above</span>
                 <code class="id">${escapeHtml(e.core.id)}</code></div></div>`;
    } else if (e.core) {
        under = `<div class="sat core-of"><div class="content">${escapeHtml(e.core.content)}</div><div class="meta">
                 <span class="badge tier-long">the core it hangs under</span> <code class="id">${escapeHtml(e.core.id)}</code></div></div>`;
    }
    return entryCard(e.tier, e.content, m, { id: e.id, under });
}
function openPicture(i) {
    const p = _gallery[i];
    if (!p) return;
    let o = $('pic-overlay');
    if (!o) {
        o = document.createElement('div');
        o.id = 'pic-overlay';
        o.className = 'pic-overlay';
        document.body.appendChild(o);
        o.addEventListener('click', (ev) => { if (ev.target === o || ev.target.closest('.pic-close')) closePicture(); });
        document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') closePicture(); });
    }
    // the title: the caption, else the file's name, else the digest's start
    const title = escapeHtml(p.caption || p.file || p.digest.slice(0, 12));
    const kind = picKind(p);
    const face = p.missing ? `<div class="empty">the bytes are gone from the folder; the memories still name it</div>`
               : STILL.has(p.mime) ? `<img alt="${title}" data-fig="${picPath(p)}">`
               : kind === 'video' ? `<video controls data-fig="${picPath(p)}"></video>`
               : kind === 'audio' ? `<audio controls data-fig="${picPath(p)}"></audio>`
               : `<a class="btn" target="_blank" rel="noopener" data-fig="${picPath(p)}">open the ${escapeHtml(kind)}</a>`;
    // where it came from: the file's name, the whole path on hover
    const from = p.source ? `<span title="${escapeHtml(p.source)}">from ${escapeHtml(p.source.split(/[\\/]/).pop())}</span>` : '';
    const facts = [p.mime, human(p.bytes), p.kept_at ? `kept ${fmtTs(p.kept_at)}` : '', `${p.digest.slice(0, 12)}…`]
                  .filter(Boolean).map(f => `<span>${escapeHtml(f)}</span>`).join('') + from;
    const n = (p.entries || []).length;
    const shown = new Set((p.entries || []).map(e => e.id));
    // A header row, then the face with room under it: the close button used
    // to float over the top-right corner, and a sound clip's controls ran
    // under it (20 Oct 2026).
    o.innerHTML = `
        <div class="pic-card" role="dialog" aria-label="${title || 'picture'}">
            <div class="pic-head">
                <span class="pic-title">${title}</span>
                <button class="pic-close" aria-label="close">✕</button>
            </div>
            <div class="face">${face}</div>
            <div class="facts">${facts}</div>
            <h3 class="group-h">attached to ${n} memor${n === 1 ? 'y' : 'ies'}</h3>
            <div class="entries">${(p.entries || []).map(e => attachedRow(e, shown)).join('')}</div>
        </div>`;
    o.classList.add('show');
    hydrateFigures(o);
}
function closePicture() { const o = $('pic-overlay'); if (o) o.classList.remove('show'); }

async function loadBriefs() {
    setHint('Daily briefs · what mattered · each one opens a sleep · newest first');
    setExtraToggle(null);
    const data = await api('/brief?limit=50');
    renderEntries((data.entries || []).map(b => briefCard(b)).join(''));
}

async function loadDrafts() {
    setHint('Hippocampus drafts · operations on long-term, reviewed one by one by the main model');
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
        const html = (data.hits || []).map(h => {
            const m = h.metadata || {};
            const n = ((h.surroundings || m.surroundings || {}).satellites) || 0;
            const under = h.tier === 'long' ? surroundingsBlock(h.id, m, null, n) : '';
            return entryCard(h.tier, h.content, m, { id: h.id, score: h.score, under });
        }).join('');
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
    const isMap = tier === 'map';
    $('searchbar').style.display = isSearch ? 'flex' : 'none';
    $('list-toolbar').style.display = (isSearch || isOverview || isMap) ? 'none' : 'flex';
    $('entries').style.display = (isOverview || isMap) ? 'none' : 'flex';
    $('overview-section').style.display = isOverview ? 'block' : 'none';
    $('map-section').style.display = isMap ? 'block' : 'none';
    document.body.classList.toggle('map-on', isMap);      // the Map fills the window: no page scroll under it
    if (isMap) { window.scrollTo(0, 0); mapFrame(); }
    if (!isMap) mapPause();
    clearError();
    try {
        if (isMap) { await loadMap(); return; }
        if (tier === 'short') await loadShort();
        else if (tier === 'near') await loadNear();
        else if (tier === 'long') await loadLong();
        else if (tier === 'eidetic') await loadEidetic();
        else if (tier === 'briefs') await loadBriefs();
        else if (tier === 'drafts') await loadDrafts();
        else if (tier === 'search') renderEntries(`<div class="empty">enter a query and hit search</div>`);
        else if (tier === 'overview') { const r = await api('/'); loadOverview(r); }
    } catch (e) { showError(e.message); renderEntries(''); }
}

// ----------------------------------------------------------------------
// The Map (23 Oct 2026): long-term as a constellation. The user wanted to see it -
// "heres the cores, heres the sats, and heres how they all hook to eachother".
// GET /long/map places every entry in 3D by what it means (its embedding, by
// PCA) and names the links: home (a satellite's first core), bridge (one
// episode supporting another story too) and history (a core's older wording
// under what replaced it). Meaning keeps those places; Constellation lets the
// links pull. A core's colour comes from where it sits, so a neighbourhood
// reads as one hue and its satellites wear their core's. The 3D library is
// vendored, served from /viewer/vendor/ and run from an object URL.
// ----------------------------------------------------------------------
const MAP = { graph: null, data: null, byId: new Map(), layout: 'meaning', sats: true,
              rotate: !matchMedia('(prefers-reduced-motion: reduce)').matches,
              selected: null, hood: null, hoodLinks: null, lit: null, lib: null, fitted: false };

function loadMapLib() {
    if (window.ForceGraph3D) return Promise.resolve();
    if (!MAP.lib) {
        MAP.lib = apiBlob('/viewer/vendor/3d-force-graph.min.js').then(url => new Promise((ok, bad) => {
            const s = document.createElement('script');
            s.src = url;
            s.onload = () => ok();
            s.onerror = () => bad(new Error('the 3D library did not load'));
            document.head.appendChild(s);
        }));
        MAP.lib.catch(() => { MAP.lib = null; });
    }
    return MAP.lib;
}

function hslRgba(h, s, l, a) {
    s /= 100; l /= 100;
    const k = n => (n + h / 30) % 12, f = n => l - s * Math.min(l, 1 - l) * Math.max(-1, Math.min(k(n) - 3, 9 - k(n), 1));
    return `rgba(${Math.round(255 * f(0))},${Math.round(255 * f(8))},${Math.round(255 * f(4))},${a})`;
}
const endId = (e) => (e && typeof e === 'object') ? e.id : e;

function mapPrepare(data) {
    MAP.byId = new Map(data.nodes.map(n => [n.id, n]));
    for (const n of data.nodes) {
        n.subs = [];
        const [x, y, z] = n.pos;
        n.x = x; n.y = y; n.z = z;
        n.hue = (Math.atan2(z, x) * 180 / Math.PI + 360 + y * 0.15) % 360;
    }
    for (const n of data.nodes) {
        if (n.kind !== 'satellite') continue;
        const home = MAP.byId.get((n.cores || [])[0]);
        if (home) n.hue = home.hue;
        for (const c of n.cores || []) { const core = MAP.byId.get(c); if (core) core.subs.push(n.id); }
    }
    mapPlace(data.nodes);
    return data;
}

function mapPlace(nodes) {
    for (const n of nodes) {
        if (MAP.layout === 'meaning') { n.fx = n.pos[0]; n.fy = n.pos[1]; n.fz = n.pos[2]; }
        else { n.fx = undefined; n.fy = undefined; n.fz = undefined; }
    }
}

function mapNodeColor(n) {
    const dim = (MAP.hood && !MAP.hood.has(n.id)) || (MAP.lit && !MAP.lit.has(n.id));
    const a = dim ? 0.1 : 1;
    if (MAP.lit && MAP.lit.has(n.id)) return `rgba(255,214,110,1)`;
    if (n.was_core) return `rgba(138,147,166,${dim ? 0.08 : 0.55})`;
    if (n.verbatim) return `rgba(255,243,196,${a})`;
    if (n.kind === 'core') return hslRgba(n.hue, 88, n.superseded_by ? 40 : 62, a);
    return hslRgba(n.hue, 70, 76, dim ? 0.07 : 0.8);
}

function mapNodeVal(n) {
    if (n.kind === 'core') return 3 + 1.6 * Math.sqrt(n.evidence || 1) + (n.verbatim ? 1 : 0);
    return n.was_core ? 0.45 : 0.8;
}

function mapLinkOn(l) { return !MAP.hoodLinks || MAP.hoodLinks.has(l); }

function mapLinkColor(l) {
    const on = mapLinkOn(l), sel = MAP.hoodLinks && on;
    if (l.kind === 'bridge') return `rgba(255,210,122,${on ? 0.9 : 0.06})`;
    if (l.kind === 'history') return `rgba(138,147,166,${on ? (sel ? 0.8 : 0.3) : 0.04})`;
    const core = MAP.byId.get(endId(l.target));
    return hslRgba(core ? core.hue : 0, 70, 70, on ? (sel ? 0.85 : 0.3) : 0.03);
}

function mapLabel(n) {
    const kind = n.kind === 'core'
        ? (n.verbatim ? 'core · kept verbatim' : `core · ${n.subs.length} episode${n.subs.length === 1 ? '' : 's'}`)
        : (n.was_core ? 'an older wording' : (n.cores || []).length > 1 ? `satellite · ${n.cores.length} stories` : 'satellite');
    const text = (n.text || '').length > 180 ? n.text.slice(0, 180) + '…' : (n.text || '');
    return `<div class="map-tip"><div class="mt-kind">${escapeHtml(kind)}</div>` +
           (n.topic ? `<div class="mt-topic">${escapeHtml(n.topic)}</div>` : '') +
           `<div class="mt-text">${escapeHtml(text)}</div></div>`;
}

// Deep space behind the scene: a few hundred stars, a handful of them warm,
// on a canvas under the transparent WebGL one - not the cores or the
// satellites, the sky they hang in.
function mapStars() {
    const c = document.createElement('canvas');
    c.className = 'map-stars';
    c.width = 2400; c.height = 1500;
    $('map-section').prepend(c);
    twinkleStars(c, starField(520, c.width, c.height));
}

// The stars far behind everything twinkle: most a little, a few a lot, each
// on its own clock, the bigger ones with a soft halo that breathes with them.
// Redrawn at ~30 frames a second, and only while the canvas is on screen and
// the tab is in front. prefers-reduced-motion gets a slower, fainter twinkle,
// not a frozen sky: nothing moves, only brightness changes, and Windows hands
// that setting to every browser on a box whose animations are turned off.
function twinkleStars(c, stars) {
    const x = c.getContext('2d');
    const calm = matchMedia('(prefers-reduced-motion: reduce)').matches ? 0.45 : 1;
    const draw = (t) => {
        x.clearRect(0, 0, c.width, c.height);
        for (const s of stars) {
            const k = 1 - s.amp * calm * (0.5 + 0.5 * Math.sin(t * s.sp * calm + s.ph));
            x.fillStyle = s.col;
            if (s.r > 1.05) {
                x.globalAlpha = s.a * k * 0.18;
                x.beginPath(); x.arc(s.x, s.y, s.r * 3.2, 0, 2 * Math.PI); x.fill();
            }
            x.globalAlpha = s.a * k;
            x.beginPath(); x.arc(s.x, s.y, s.r, 0, 2 * Math.PI); x.fill();
        }
        x.globalAlpha = 1;
    };
    draw(0);
    let last = 0;
    const tick = (now) => {
        requestAnimationFrame(tick);
        if (now - last < 33 || document.hidden || !c.getClientRects().length) return;
        last = now;
        draw(now / 1000);
    };
    requestAnimationFrame(tick);
}

function starField(n, w, h, yOf = () => Math.random() * h) {
    const out = [];
    for (let i = 0; i < n; i++) {
        const big = Math.random() < 0.06, warm = Math.random() < 0.12, lively = Math.random() < 0.15;
        out.push({ x: Math.random() * w, y: yOf(), r: big ? 1.1 + Math.random() * 0.9 : 0.3 + Math.random() * 0.7,
                   col: warm ? '#ffd6aa' : '#d2e1ff', a: warm ? 0.35 + Math.random() * 0.5 : 0.2 + Math.random() * 0.6,
                   amp: lively ? 0.55 + Math.random() * 0.4 : 0.1 + Math.random() * 0.2,
                   sp: 0.6 + Math.random() * 1.8, ph: Math.random() * 6.283 });
    }
    return out;
}


// zoomToFit frames the whole bounding box, depth included, which leaves a
// round cloud small on a wide screen: fit, then come in a little closer
function mapFit(ms = 900) {
    const g = MAP.graph;
    if (!g) return;
    g.zoomToFit(ms, 10);
    setTimeout(() => {
        const p = g.cameraPosition(), k = 0.72;
        g.cameraPosition({ x: p.x * k, y: p.y * k, z: p.z * k }, undefined, 700);
    }, ms + 60);
}

function mapBuild() {
    mapStars();
    const el = $('map-canvas');
    const g = ForceGraph3D({ controlType: 'orbit' })(el)
        .backgroundColor('rgba(0,0,0,0)')
        .showNavInfo(false)
        .nodeRelSize(3)
        .nodeResolution(14)
        .nodeOpacity(0.95)
        .nodeVal(mapNodeVal)
        .nodeColor(mapNodeColor)
        .nodeLabel(mapLabel)
        .nodeVisibility(n => MAP.sats || n.kind === 'core')
        .linkVisibility(l => MAP.sats)
        .linkColor(mapLinkColor)
        .linkOpacity(1)
        .linkWidth(l => (l.kind === 'bridge' ? 0.9 : 0.25) * (MAP.hoodLinks && MAP.hoodLinks.has(l) ? 2.6 : 1))
        .linkDirectionalParticles(l => l.kind === 'bridge' ? 3 : 0)
        .linkDirectionalParticleWidth(1.5)
        .linkDirectionalParticleSpeed(0.0045)
        .linkDirectionalParticleColor(() => '#ffe3a3')
        .cooldownTicks(MAP.layout === 'meaning' ? 1 : 260)
        .onNodeHover(n => { el.style.cursor = n ? 'pointer' : ''; })
        .onNodeClick(n => mapSelect(n.id))
        .onBackgroundClick(() => mapSelect(null))
        .onEngineStop(() => { if (!MAP.fitted) { MAP.fitted = true; mapFit(); } });
    g.d3Force('charge').strength(-16);
    g.d3Force('link').distance(l => l.kind === 'bridge' ? 70 : l.kind === 'history' ? 14 : 24).strength(l => l.kind === 'bridge' ? 0.15 : 0.6);
    const c = g.controls();
    c.autoRotate = MAP.rotate;
    c.autoRotateSpeed = 0.45;
    el.addEventListener('pointerdown', () => { if (MAP.rotate) { MAP.rotate = false; c.autoRotate = false; $('map-rotate').checked = false; } });
    new ResizeObserver(() => { if (el.clientWidth) g.width(el.clientWidth).height(el.clientHeight); }).observe(el);
    MAP.graph = g;
    mapWire();
}

function mapRefresh() {
    const g = MAP.graph;
    if (!g) return;
    g.nodeColor(g.nodeColor()).linkColor(g.linkColor()).linkWidth(g.linkWidth())
     .nodeVisibility(g.nodeVisibility()).linkVisibility(g.linkVisibility());
}

function mapSelect(id) {
    const n = id ? MAP.byId.get(id) : null;
    MAP.selected = n;
    if (!n) {
        MAP.hood = MAP.hoodLinks = null;
        $('map-info').hidden = true;
        mapRefresh();
        return;
    }
    const hood = new Set([n.id, ...(n.cores || []), ...n.subs]);
    if (n.superseded_by) hood.add(n.superseded_by);
    MAP.hood = hood;
    MAP.hoodLinks = new Set(MAP.data.links.filter(l => endId(l.source) === n.id || endId(l.target) === n.id));
    mapRefresh();
    mapInfo(n);
    const d = n.kind === 'core' ? 110 : 70, r = Math.hypot(n.x, n.y, n.z) || 1, k = 1 + d / r;
    MAP.graph.cameraPosition({ x: n.x * k, y: n.y * k, z: n.z * k }, { x: n.x, y: n.y, z: n.z }, 1400);
}

function mapRef(id) {
    const m = MAP.byId.get(id);
    if (!m) return `<li class="faint">${escapeHtml(id.slice(0, 8))} (not on the map)</li>`;
    const t = (m.text || '').length > 110 ? m.text.slice(0, 110) + '…' : m.text;
    const sw = m.was_core ? 'rgba(138,147,166,1)' : m.verbatim ? 'rgba(255,243,196,1)' : hslRgba(m.hue, 80, 66, 1);
    return `<li><button class="mi-ref" data-id="${escapeHtml(m.id)}"><i style="background:${sw}"></i>${escapeHtml(t)}</button></li>`;
}

function mapInfo(n) {
    const kind = n.kind === 'core'
        ? (n.verbatim ? 'core · kept verbatim' : n.superseded_by ? 'core · replaced' : 'core')
        : n.was_core ? 'an older wording' : 'satellite';
    const facts = [kind, `evidence ${n.evidence}`, n.pictures ? `${n.pictures} picture${n.pictures === 1 ? '' : 's'}` : '']
        .filter(Boolean).map(escapeHtml).join(' · ');
    const homes = (n.cores || []);
    const subs = n.subs.filter(s => !MAP.byId.get(s)?.was_core), olds = n.subs.filter(s => MAP.byId.get(s)?.was_core);
    $('map-info').innerHTML = `
        <button class="mi-close" aria-label="Close" data-close>×</button>
        <div class="mi-kind">${facts}</div>
        ${n.topic ? `<div class="mi-topic">${escapeHtml(n.topic)}</div>` : ''}
        <div class="mi-text">${escapeHtml(n.text || '')}</div>
        <div class="mi-when">${escapeHtml(fmtTs(n.created_at))} · <span class="mono">${escapeHtml(n.id.slice(0, 8))}</span></div>
        ${homes.length ? `<h4>${n.was_core ? 'replaced by' : homes.length > 1 ? `hangs on ${homes.length} stories` : 'hangs on'}</h4><ul>${homes.map(mapRef).join('')}</ul>` : ''}
        ${subs.length ? `<h4>its episodes (${subs.length})</h4><ul>${subs.map(mapRef).join('')}</ul>` : ''}
        ${olds.length ? `<h4>what it said before</h4><ul>${olds.map(mapRef).join('')}</ul>` : ''}`;
    $('map-info').hidden = false;
    $('map-info').scrollTop = 0;
}

function mapLight(q) {
    q = (q || '').trim().toLowerCase();
    MAP.lit = q ? new Set(MAP.data.nodes.filter(n => (n.text || '').toLowerCase().includes(q) ||
                                                    (n.topic || '').toLowerCase().includes(q)).map(n => n.id)) : null;
    mapRefresh();
    mapStats();
}

function mapStats() {
    const c = MAP.data.counts;
    $('map-stats').textContent = `${c.cores} cores · ${c.satellites} satellites · ${c.bridges} bridges` +
        (MAP.lit ? ` · ${MAP.lit.size} lit` : '');
}

function mapWire() {
    document.querySelectorAll('.map-seg-btn').forEach(b => b.addEventListener('click', () => {
        if (MAP.layout === b.dataset.layout) return;
        MAP.layout = b.dataset.layout;
        document.querySelectorAll('.map-seg-btn').forEach(x => x.classList.toggle('on', x === b));
        mapPlace(MAP.data.nodes);
        MAP.graph.cooldownTicks(MAP.layout === 'meaning' ? 1 : 260).d3ReheatSimulation();
        MAP.fitted = false;
    }));
    $('map-sats').addEventListener('change', (e) => { MAP.sats = e.target.checked; mapRefresh(); });
    $('map-rotate').addEventListener('change', (e) => { MAP.rotate = e.target.checked; MAP.graph.controls().autoRotate = MAP.rotate; });
    $('map-search').addEventListener('input', (e) => mapLight(e.target.value));
    $('map-fit').addEventListener('click', () => mapFit(800));
    $('map-info').addEventListener('click', (e) => {
        if (e.target.closest('[data-close]')) return mapSelect(null);
        const ref = e.target.closest('.mi-ref');
        if (ref) mapSelect(ref.dataset.id);
    });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && state.tab === 'map') mapSelect(null); });
}

async function loadMap() {
    $('map-loading').hidden = false;
    $('map-loading').textContent = 'charting…';
    try {
        const [, data] = await Promise.all([loadMapLib(), api('/long/map')]);
        MAP.data = mapPrepare(data);
        $('map-rotate').checked = MAP.rotate;
        if (!MAP.graph) mapBuild();
        MAP.fitted = false;
        MAP.graph.cooldownTicks(MAP.layout === 'meaning' ? 1 : 260).graphData({ nodes: data.nodes, links: data.links });
        MAP.graph.resumeAnimation();
        if (MAP.layout === 'meaning') setTimeout(() => { MAP.fitted = true; mapFit(); }, 400);
        mapLight($('map-search').value);
        if (MAP.selected) mapSelect(MAP.byId.has(MAP.selected.id) ? MAP.selected.id : null);
        $('map-loading').hidden = true;
    } catch (e) {
        $('map-loading').textContent = '';
        $('map-loading').hidden = true;
        throw e;
    }
}

function mapPause() { if (MAP.graph) MAP.graph.pauseAnimation(); }

function mapFrame() {
    const bar = document.querySelector('.tabbar') || document.querySelector('header');
    if (bar) $('map-section').style.top = Math.round(bar.getBoundingClientRect().bottom) + 'px';
}
window.addEventListener('resize', () => { if (state.tab === 'map') mapFrame(); });

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
