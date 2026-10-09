(() => {
'use strict';

const BASE = '/refused';
const MODULES = ['stream_strip', 'poster_refresh', 'see_all', 'search_clean', 'diagnostics'];

let csrf = '';
let S = null;              // last /api/state
const drafts = {};         // module -> unsaved working copy of its settings
const notices = {};        // module -> one-shot message shown after a re-render
let live = [];             // callbacks re-run after each state refresh
let pollTimer = null;
let logTimer = null;

const $ = (sel) => document.querySelector(sel);
const view = $('#view');
const nav = $('#nav');

/* ---------------------------------------------------------------- helpers */
function h(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
        if (v === false || v == null) continue;
        if (k === 'class') n.className = v;
        else if (k === 'text') n.textContent = v;
        else if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2), v);
        else n.setAttribute(k, v === true ? '' : String(v));
    }
    for (const kid of kids.flat(Infinity)) {
        if (kid == null || kid === false) continue;
        n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return n;
}

const clone = (o) => JSON.parse(JSON.stringify(o));
const label = (s) => s.toUpperCase();

function fmtTime(ts) {
    const d = new Date(ts * 1000);
    return d.toTimeString().slice(0, 8);
}
function fmtDate(ts) {
    return ts ? new Date(ts * 1000).toLocaleString() : '-';
}
function fmtBytes(n) {
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
    if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
    return (n / 1073741824).toFixed(2) + ' GB';
}
function fmtUptime(s) {
    const d = Math.floor(s / 86400), hh = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return (d ? d + 'd ' : '') + (hh || d ? hh + 'h ' : '') + m + 'm';
}
function statValue(key, v) {
    if (key.includes('bytes')) return fmtBytes(v);
    return typeof v === 'number' ? v.toLocaleString() : String(v);
}

async function api(method, path, body) {
    const opts = { method, headers: {}, credentials: 'same-origin' };
    if (body !== undefined) {
        opts.headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(body);
    }
    if (method !== 'GET' && csrf) opts.headers['X-CSRF-Token'] = csrf;
    let res;
    try {
        res = await fetch(BASE + '/api' + path, opts);
    } catch (e) {
        throw new Error('CONNECTION LOST');
    }
    let data = null;
    try { data = await res.json(); } catch (e) { /* not JSON */ }
    if (res.status === 401 && path !== '/login') {
        stopTimers();
        showLogin('SESSION EXPIRED');
        throw new Error('SESSION EXPIRED');
    }
    if (!res.ok) {
        const msg = data && (data.errors ? data.errors.join(' | ') : data.error);
        const err = new Error(msg || res.statusText);
        err.status = res.status;
        throw err;
    }
    return data;
}

function flash(box, kind, text) {
    box.className = 'msg ' + kind;
    box.textContent = text;
    box.hidden = false;
    if (kind === 'ok') {
        clearTimeout(box._t);
        box._t = setTimeout(() => { box.hidden = true; }, 5000);
    }
}

function stopTimers() {
    clearInterval(pollTimer); pollTimer = null;
    clearInterval(logTimer); logTimer = null;
}

/* ------------------------------------------------------------------ login */
function showLogin(msg) {
    stopTimers();
    S = null; csrf = '';
    nav.hidden = true;
    const err = h('div', { class: 'msg err', hidden: !msg, role: 'alert' }, msg || '');
    const user = h('input', { type: 'text', autocomplete: 'username', autocapitalize: 'off', spellcheck: 'false', id: 'u' });
    const pass = h('input', { type: 'password', autocomplete: 'current-password', id: 'p' });
    const go = h('button', { class: 'btn', type: 'submit' }, '> AUTHENTICATE');
    const form = h('form', { class: 'panel login', novalidate: true },
        h('span', { class: 'panel-title' }, 'ACCESS_CONTROL'),
        h('div', { class: 'field' }, h('div', { class: 'field-row' },
            h('label', { for: 'u' }, '> USERNAME:'), h('div', { class: 'control' }, user))),
        h('div', { class: 'field' }, h('div', { class: 'field-row' },
            h('label', { for: 'p' }, '> PASSWORD:'), h('div', { class: 'control' }, pass))),
        h('div', { class: 'btn-group' }, go),
        err,
    );
    form.addEventListener('submit', async (ev) => {
        ev.preventDefault();
        go.disabled = true;
        try {
            const r = await api('POST', '/login', { username: user.value, password: pass.value });
            csrf = r.csrf;
            await enter();
        } catch (e) {
            pass.value = '';
            flash(err, 'err', e.message);
            go.disabled = false;
            pass.focus();
        }
    });
    view.replaceChildren(form);
    user.focus();
}

async function logout() {
    try { await api('POST', '/logout', {}); } catch (e) { /* already gone */ }
    showLogin();
}

/* --------------------------------------------------------------- app shell */
async function enter() {
    S = await api('GET', '/state');
    nav.hidden = false;
    startPolling();
    route();
}

function startPolling() {
    clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
        if (document.hidden) return;
        try {
            S = await api('GET', '/state');
            live.forEach((fn) => fn());
        } catch (e) { /* shown elsewhere */ }
    }, 4000);
}

function buildNav(current) {
    const items = [
        ['status', 'STATUS'],
        ['stream_strip', 'STREAM_STRIP'],
        ['poster_refresh', 'POSTER_REFRESH'],
        ['see_all', 'SEE_ALL'],
        ['search_clean', 'SEARCH_CLEAN'],
        ['diagnostics', 'DIAGNOSTICS'],
        ['log', 'LOG'],
    ];
    nav.replaceChildren(
        ...items.map(([key, text]) =>
            h('a', { href: '#/' + key, class: key === current ? 'active' : '', 'aria-current': key === current ? 'page' : false }, `[ ${text} ]`)),
        h('a', { href: '#/exit', onclick: (ev) => { ev.preventDefault(); logout(); } }, '[ EXIT ]'),
    );
}

function route() {
    if (!S) return;
    live = [];
    clearInterval(logTimer); logTimer = null;
    const key = (location.hash.replace(/^#\//, '') || 'status');
    const current = key === 'log' || MODULES.includes(key) ? key : 'status';
    buildNav(current);
    if (current === 'status') renderStatus();
    else if (current === 'log') renderLog();
    else renderModule(current);
    window.scrollTo(0, 0);
}
window.addEventListener('hashchange', route);

/* ------------------------------------------------------------- shared bits */
function statsList(module) {
    const dl = h('dl', { class: 'stats' });
    const paint = () => {
        const stats = S.modules[module].stats;
        dl.replaceChildren(...Object.entries(stats).flatMap(([k, v]) => [
            h('dt', null, label(k)),
            h('dd', null, statValue(k, v)),
        ]));
    };
    paint();
    live.push(paint);
    return dl;
}

async function setEnabled(module, on, btn) {
    btn.disabled = true;
    try {
        const r = await api('PUT', '/settings/' + module, { enabled: on });
        S.modules[module].settings = r.settings;
    } catch (e) {
        alert(e.message);
    }
    route();
}

function switchButton(module) {
    const on = S.modules[module].settings.enabled;
    const btn = h('button', { class: 'switch ' + (on ? 'on' : 'off'), type: 'button', 'aria-pressed': String(on) },
        on ? '[ ENABLED ]' : '[ DISABLED ]');
    btn.addEventListener('click', () => setEnabled(module, !on, btn));
    return btn;
}

/* ------------------------------------------------------------------ status */
function renderStatus() {
    const up = S.upstream;
    const upText = h('span', null);
    const kv = h('div', { class: 'kv' });
    const paint = () => {
        const u = S.upstream;
        upText.textContent = u.reachable ? `ONLINE (HTTP ${u.http_status})` : `UNREACHABLE (${u.error})`;
        kv.replaceChildren(
            h('div', null, 'REMUX'), h('div', null, u.url),
            h('div', null, 'STATE'), h('div', null, upText),
            h('div', null, 'PROXIED'), h('div', null, S.proxy.requests.toLocaleString() + ' requests'),
            h('div', null, 'ERRORS'), h('div', null, S.proxy.upstream_errors.toLocaleString()),
            h('div', null, 'UPTIME'), h('div', null, fmtUptime(S.uptime_seconds)),
            ...(u.version ? [h('div', null, 'REMUX_VERSION'), h('div', null, u.version)] : []),
        );
    };
    paint();
    live.push(paint);

    const rows = MODULES.map((m) => {
        const info = S.schema[m];
        const st = S.modules[m];
        const summary = h('span', null);
        const paintRow = () => {
            const s = S.modules[m].stats;
            if (m === 'stream_strip') summary.textContent = `${s.responses_stripped.toLocaleString()} responses cleaned`;
            else if (m === 'see_all') summary.textContent = `${s.requests_expanded.toLocaleString()} 'see all' requests expanded, ${s.pages_served.toLocaleString()} pages served`;
            else if (m === 'search_clean') summary.textContent = `${s.people_searches_hidden.toLocaleString()} people searches hidden`;
            else if (m === 'diagnostics') summary.textContent = `${s.requests_logged.toLocaleString()} requests logged`;
            else summary.textContent = `${s.tracked_images.toLocaleString()} images tracked, ${s.changes_detected.toLocaleString()} changes detected`;
        };
        paintRow();
        live.push(paintRow);
        return h('div', { class: 'module-row' },
            h('div', { class: 'module-head' },
                h('div', null,
                    h('div', { class: 'module-name' }, '> ' + info.label),
                    h('div', { class: 'module-tag' }, info.tagline)),
                switchButton(m)),
            h('div', { class: 'module-foot' }, summary,
                h('a', { href: '#/' + m }, '[ CONFIGURE ]')),
        );
    });

    view.replaceChildren(
        h('section', { class: 'panel' },
            h('span', { class: 'panel-title' }, 'MODULES'),
            h('p', { class: 'lead' }, 'Switch tools on or off. Changes apply to the very next request; nothing needs a restart.'),
            ...rows),
        h('section', { class: 'panel' },
            h('span', { class: 'panel-title' }, 'UPSTREAM'), kv),
        h('section', { class: 'panel' },
            h('span', { class: 'panel-title' }, 'CONNECT_INFUSE'),
            h('ol', { class: 'steps' },
                h('li', null, 'In Infuse, add or edit your Remux server and use this address instead of Remux directly: ',
                    h('span', { class: 'mono' }, location.origin.replace(/^https?:\/\//, ''))),
                h('li', null, 'Keep your normal Remux username and password. Refused passes them straight through.'),
                h('li', null, 'With every module off, Refused is a transparent proxy and Infuse sees no difference.'))),
    );
}

/* ---------------------------------------------------------------- settings */
function savedOf(module) {
    const s = clone(S.modules[module].settings);
    delete s.enabled;
    return s;
}
function draftOf(module) {
    if (!drafts[module]) drafts[module] = savedOf(module);
    return drafts[module];
}

function buildControl(spec, draft, markDirty) {
    const key = spec.key;
    const wrap = h('div', { class: 'control' });
    let optHelp = null;

    if (spec.type === 'bool') {
        const btn = h('button', { class: 'chip', type: 'button', 'aria-pressed': String(!!draft[key]) },
            draft[key] ? '[X] ON' : '[ ] OFF');
        btn.addEventListener('click', () => {
            draft[key] = !draft[key];
            btn.setAttribute('aria-pressed', String(draft[key]));
            btn.textContent = draft[key] ? '[X] ON' : '[ ] OFF';
            markDirty();
        });
        wrap.append(btn);
    } else if (spec.type === 'int') {
        const input = h('input', { type: 'number', inputmode: 'numeric', min: spec.min, max: spec.max, step: 1, value: draft[key], id: 'f_' + key });
        input.addEventListener('input', () => {
            draft[key] = input.value === '' ? null : Number(input.value);
            markDirty();
        });
        wrap.append(input);
        if (spec.unit) wrap.append(h('span', { class: 'unit' }, spec.unit + (spec.min != null ? `  (${spec.min}-${spec.max})` : '')));
    } else if (spec.type === 'select') {
        const sel = h('select', { id: 'f_' + key },
            spec.options.map((o) => h('option', { value: o.value }, o.label)));
        sel.value = draft[key];
        optHelp = h('p', { class: 'help opt' });
        const paintOpt = () => {
            const o = spec.options.find((x) => x.value === draft[key]);
            optHelp.textContent = o && o.help ? o.help : '';
            optHelp.hidden = !(o && o.help);
        };
        paintOpt();
        sel.addEventListener('change', () => { draft[key] = sel.value; paintOpt(); markDirty(); });
        wrap.append(sel);
    } else if (spec.type === 'multiselect') {
        const chips = h('div', { class: 'chips', role: 'group', 'aria-label': spec.label });
        spec.options.forEach((o) => {
            const on = draft[key].includes(o.value);
            const chip = h('button', { class: 'chip', type: 'button', 'aria-pressed': String(on) }, `${on ? '[X]' : '[ ]'} ${o.label}`);
            chip.addEventListener('click', () => {
                const i = draft[key].indexOf(o.value);
                if (i >= 0) draft[key].splice(i, 1); else draft[key].push(o.value);
                const now = draft[key].includes(o.value);
                chip.setAttribute('aria-pressed', String(now));
                chip.textContent = `${now ? '[X]' : '[ ]'} ${o.label}`;
                markDirty();
            });
            chips.append(chip);
        });
        wrap.append(chips);
    } else if (spec.type === 'lines') {
        const ta = h('textarea', { id: 'f_' + key, rows: 11, spellcheck: 'false', autocapitalize: 'off' });
        ta.value = draft[key].join('\n');
        ta.addEventListener('input', () => {
            draft[key] = ta.value.split('\n').map((x) => x.trim()).filter(Boolean);
            markDirty();
        });
        wrap.append(ta);
    }
    return { wrap, optHelp };
}

function renderForm(module, onSaved) {
    const info = S.schema[module];
    const draft = draftOf(module);
    const dirtyTag = h('span', { class: 'dirty', hidden: true }, 'UNSAVED CHANGES');
    const apply = h('button', { class: 'btn', type: 'button', disabled: true }, '> APPLY');
    const reset = h('button', { class: 'btn warn', type: 'button' }, '> RESTORE_DEFAULTS');
    const msg = h('div', { class: 'msg', hidden: true, role: 'status' });
    if (notices[module]) { flash(msg, 'ok', notices[module]); delete notices[module]; }

    const markDirty = () => {
        const d = JSON.stringify(draft) !== JSON.stringify(savedOf(module));
        dirtyTag.hidden = !d;
        apply.disabled = !d;
    };

    const fields = info.fields.map((spec) => {
        const { wrap, optHelp } = buildControl(spec, draft, markDirty);
        const idFor = ['int', 'select', 'lines'].includes(spec.type) ? 'f_' + spec.key : null;
        return h('div', { class: 'field' },
            h('div', { class: 'field-row' },
                h('label', { for: idFor }, '> ' + spec.label + ':'),
                wrap),
            h('p', { class: 'help' }, spec.help),
            optHelp);
    });

    apply.addEventListener('click', async () => {
        apply.disabled = true;
        try {
            const r = await api('PUT', '/settings/' + module, draft);
            S.modules[module].settings = r.settings;
            Object.assign(draft, savedOf(module));   // keep object identity: the controls hold it
            markDirty();
            flash(msg, 'ok', 'SETTINGS APPLIED. LIVE ON THE NEXT REQUEST.');
        } catch (e) {
            flash(msg, 'err', e.message);
            markDirty();
        }
    });
    reset.addEventListener('click', async () => {
        if (!confirm('Restore every ' + info.label + ' setting to its default?')) return;
        try {
            const r = await api('PUT', '/settings/' + module, { reset: true });
            S.modules[module].settings = r.settings;
            delete drafts[module];
            notices[module] = 'DEFAULTS RESTORED.';
            onSaved();
        } catch (e) {
            flash(msg, 'err', e.message);
        }
    });

    return h('section', { class: 'panel' },
        h('span', { class: 'panel-title' }, 'SETTINGS'),
        ...fields,
        h('div', { class: 'btn-group' }, apply, reset, dirtyTag),
        msg);
}

/* ------------------------------------------------------------ module pages */
function renderModule(module) {
    const info = S.schema[module];
    const rerender = () => renderModule(module);

    const header = h('section', { class: 'panel' },
        h('span', { class: 'panel-title' }, info.label),
        h('p', { class: 'lead' }, info.description),
        h('div', { class: 'btn-group flush' }, switchButton(module)));

    const parts = [header, renderForm(module, rerender)];
    if (module === 'poster_refresh') parts.push(renderPosterTools());
    parts.push(h('section', { class: 'panel' },
        h('span', { class: 'panel-title' }, 'STATISTICS'), statsList(module)));
    view.replaceChildren(...parts);
}

function renderPosterTools() {
    const msg = h('div', { class: 'msg', hidden: true, role: 'status' });
    const out = h('pre', { class: 'out', hidden: true });
    const itemId = h('input', { type: 'text', id: 'probe_id', placeholder: 'item id (UUID)', spellcheck: 'false', autocapitalize: 'off' });
    const imgType = h('select', { id: 'probe_type' },
        ['Primary', 'Backdrop', 'Logo', 'Thumb'].map((t) => h('option', { value: t }, t)));
    const probe = h('button', { class: 'btn small', type: 'button' }, '> PROBE');

    probe.addEventListener('click', async () => {
        out.hidden = true;
        probe.disabled = true;
        try {
            const r = await api('POST', '/poster/probe', { item_id: itemId.value.trim(), image_type: imgType.value });
            const p = r.probe;
            const lines = [];
            if (p.ok) {
                lines.push('LIVE FETCH');
                lines.push(`  size ........ ${fmtBytes(p.bytes)}${p.width ? `  (${p.width}x${p.height} ${p.format || ''})` : ''}`);
                lines.push(`  type ........ ${p.content_type}`);
                lines.push(`  sha256 ...... ${p.sha256}`);
                lines.push(`  matches ..... ${p.matches_recorded == null ? 'not tracked yet' : (p.matches_recorded ? 'recorded fingerprint' : 'DIFFERS from recorded fingerprint')}`);
            } else {
                lines.push('LIVE FETCH FAILED: ' + p.error);
            }
            lines.push('');
            if (r.records.length) {
                lines.push('TRACKED STATE');
                r.records.forEach((x) => {
                    lines.push(`  ${x.image_type}: remux tag ${x.remux_tag.slice(0, 12)}  emitted ${x.emitted_tag.slice(0, 12)}  v${x.version}  fp ${x.fingerprint || '-'}`);
                    lines.push(`     last check ${fmtDate(x.last_checked)}  next ${fmtDate(x.next_check)}  failures ${x.failures}`);
                });
            } else {
                lines.push('TRACKED STATE: nothing yet (the item has not appeared in a response while enabled)');
            }
            out.textContent = lines.join('\n');
            out.hidden = false;
        } catch (e) {
            flash(msg, 'err', e.message);
        }
        probe.disabled = false;
    });

    const force = h('button', { class: 'btn warn', type: 'button' }, '> FORCE_REFRESH_ALL');
    force.addEventListener('click', async () => {
        if (!confirm('Give EVERY tracked image a new tag?\n\nInfuse may re-download all artwork and keep the old copies in its cache until you clear it. Use this to test whether Infuse reacts to tag changes, or after a big poster overhaul.')) return;
        try {
            const r = await api('POST', '/poster/force-refresh', {});
            flash(msg, 'ok', 'ALL TAGS CHANGED (epoch ' + r.epoch + ').');
        } catch (e) { flash(msg, 'err', e.message); }
    });
    const purge = h('button', { class: 'btn warn', type: 'button' }, '> PURGE_STATE');
    purge.addEventListener('click', async () => {
        if (!confirm('Forget everything Refused has learned?\n\nAll tags revert to the ones Remux sends, and every image is re-baselined silently on its next sighting.')) return;
        try {
            const r = await api('POST', '/poster/purge', {});
            flash(msg, 'ok', 'FORGOT ' + r.forgotten + ' TRACKED IMAGES.');
        } catch (e) { flash(msg, 'err', e.message); }
    });

    return h('section', { class: 'panel' },
        h('span', { class: 'panel-title' }, 'TOOLS'),
        h('p', { class: 'lead' }, 'PROBE fetches one image right now and shows what Refused has recorded for it, without changing anything. Find an item id in an Infuse or Remux URL.'),
        h('div', { class: 'field-row' }, itemId, imgType, probe),
        out,
        h('p', { class: 'lead gap' },
            'FORCE_REFRESH_ALL gives every tracked image a new tag once (handy to test Infuse). PURGE_STATE forgets everything and returns to Remux\'s own tags.'),
        h('div', { class: 'btn-group tight' }, force, purge),
        msg);
}

/* --------------------------------------------------------------------- log */
function renderLog() {
    const box = h('div', { class: 'log-window', role: 'log', 'aria-live': 'off' });
    let last = 0;
    let paused = false;
    const pauseBtn = h('button', { class: 'btn small', type: 'button' }, '> PAUSE');
    const clearBtn = h('button', { class: 'btn small', type: 'button' }, '> CLEAR_VIEW');
    const cursor = h('span', { class: 'cursor' });

    pauseBtn.addEventListener('click', () => {
        paused = !paused;
        pauseBtn.textContent = paused ? '> RESUME' : '> PAUSE';
    });
    clearBtn.addEventListener('click', () => { box.replaceChildren(cursor); });

    const pull = async () => {
        if (paused || document.hidden) return;
        try {
            const r = await api('GET', '/events?after=' + last);
            if (!r.events.length) return;
            const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
            cursor.remove();
            r.events.forEach((e) => {
                last = Math.max(last, e.id);
                box.append(h('div', { class: 'log-entry log-' + e.level },
                    h('span', { class: 'log-time' }, `[${fmtTime(e.ts)}]`),
                    h('span', { class: 'log-cat' }, e.cat),
                    e.msg));
            });
            while (box.children.length > 600) box.removeChild(box.firstChild);
            box.append(cursor);
            if (stick) box.scrollTop = box.scrollHeight;
        } catch (e) { /* surfaced by api() */ }
    };

    box.append(cursor);
    view.replaceChildren(
        h('section', { class: 'panel' },
            h('span', { class: 'panel-title' }, 'SYSTEM_LOGS'),
            box,
            h('div', { class: 'btn-group' }, pauseBtn, clearBtn)));
    pull().then(() => { box.scrollTop = box.scrollHeight; });
    logTimer = setInterval(pull, 1500);
}

/* -------------------------------------------------------------------- boot */
(async function boot() {
    try {
        const res = await fetch(BASE + '/api/session', { credentials: 'same-origin' });
        const s = await res.json();
        $('#subtitle').textContent = `MULTI-TOOL SIDECAR :: v${s.version} :: REMUX x INFUSE`;
        if (s.authenticated) {
            csrf = s.csrf;
            await enter();
        } else {
            showLogin();
        }
    } catch (e) {
        showLogin('CANNOT REACH REFUSED');
    }
})();
})();
