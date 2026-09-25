/* Job Agent dashboard. Plain JavaScript against the same-origin API; no build
 * step and nothing loaded from elsewhere. Every string that came from a job
 * board or an email goes onto the page through textContent (el()), never as
 * HTML, since postings and replies are written by strangers. */
(function () {
  'use strict';

  const TZ = -new Date().getTimezoneOffset();
  const STATUS_TONE = {
    applied: 'info', screening: 'good', interviewing: 'good', offer: 'good',
    rejected: 'bad', withdrawn: '', failed: 'bad', blocked: 'warn', unconfirmed: 'warn',
    needs_input: 'warn', review: 'info', dry_run: '', pending: '',
    queued: 'info', skipped: '', needs_review: 'warn',
  };
  const STATUS_LABEL = {
    applied: 'Applied', screening: 'Screening', interviewing: 'Interviewing', offer: 'Offer',
    rejected: 'Rejected', withdrawn: 'Withdrawn', failed: 'Failed', blocked: 'Blocked',
    unconfirmed: 'Unconfirmed', needs_input: 'Needs answers', review: 'To approve',
    dry_run: 'Filled, not sent', pending: 'Pending', queued: 'Queued', skipped: 'Skipped',
    needs_review: 'Needs a look',
  };
  const SITE_LABEL = {
    linkedin: 'LinkedIn', indeed: 'Indeed', greenhouse: 'Greenhouse', lever: 'Lever',
    ashby: 'Ashby', workday: 'Workday', generic: 'Company site', other: 'Other',
  };

  const state = { days: 30, appStatus: '', jobStatus: 'queued', selectedApp: null, config: null };

  // ------------------------------------------------------------ helpers --

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? '' : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function svg(tag, attrs, ...children) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, value);
    for (const child of children.flat()) {
      if (child === null || child === undefined) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  async function api(path, options) {
    const opts = Object.assign({ headers: {} }, options || {});
    if (opts.json !== undefined) {
      opts.body = JSON.stringify(opts.json);
      opts.headers['Content-Type'] = 'application/json';
      delete opts.json;
    }
    const response = await fetch(path, opts);
    if (!response.ok) {
      let detail = response.statusText;
      try { const body = await response.json(); detail = body.detail || detail; } catch (e) { /* not JSON */ }
      const error = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
      error.status = response.status;
      throw error;
    }
    if (response.status === 204) return null;
    return response.json();
  }

  let toastTimer = null;
  function toast(message, bad) {
    const box = document.getElementById('toast');
    box.textContent = message;
    box.className = bad ? 'bad' : '';
    box.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { box.hidden = true; }, bad ? 7000 : 4000);
  }

  function badge(status) {
    return el('span', { class: 'badge ' + (STATUS_TONE[status] || '') }, STATUS_LABEL[status] || status);
  }

  function siteName(site) { return SITE_LABEL[site] || site || 'Other'; }

  function pct(value) { return value === null || value === undefined ? '–' : Math.round(value * 100) + '%'; }

  function when(ts) {
    if (!ts) return '';
    const date = new Date(ts);
    if (isNaN(date)) return ts;
    const seconds = (Date.now() - date.getTime()) / 1000;
    if (seconds < 60) return 'just now';
    if (seconds < 3600) return Math.round(seconds / 60) + 'm ago';
    if (seconds < 86400) return Math.round(seconds / 3600) + 'h ago';
    if (seconds < 86400 * 7) return Math.round(seconds / 86400) + 'd ago';
    return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  function shortDate(iso) {
    const [y, m, d] = iso.split('-').map(Number);
    return new Date(y, m - 1, d).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  function splitList(text) {
    return String(text || '').split(/[\n,]+/).map((s) => s.trim()).filter(Boolean);
  }

  function busy(button, promise) {
    if (button) button.disabled = true;
    return promise.finally(() => { if (button) button.disabled = false; });
  }

  // ------------------------------------------------------------- routing --

  const VIEWS = ['overview', 'applications', 'jobs', 'profile', 'settings'];
  const LOADERS = {};

  function route() {
    const [name, query] = (location.hash.slice(1) || 'overview').split('?');
    const view = VIEWS.includes(name) ? name : 'overview';
    const params = new URLSearchParams(query || '');
    if (view === 'applications' && params.has('status')) state.appStatus = params.get('status');
    for (const v of VIEWS) document.getElementById('view-' + v).hidden = v !== view;
    for (const link of document.querySelectorAll('.tabs a')) {
      link.classList.toggle('active', link.dataset.tab === view);
    }
    LOADERS[view]().catch((e) => toast(e.message, true));
  }

  // ------------------------------------------------------------ overview --

  LOADERS.overview = async function () {
    const [stats, activity] = await Promise.all([
      api(`/api/stats?days=${state.days}&tz_offset_minutes=${TZ}`),
      api('/api/activity?limit=12'),
    ]);
    renderAttention(stats.attention);
    renderKpis(stats);
    renderPerDay(stats.per_day);
    const w = stats.window_totals;
    document.getElementById('window-note').textContent =
      `Last ${stats.window.days} days: ${w.applied} applied, ${w.responses} heard back, ${w.discovered} found.`;
    renderFunnel(stats.funnel);
    renderCategories(stats.by_category);
    renderSites(stats.by_site);
    renderActivity(activity);
  };

  function renderAttention(a) {
    const box = document.getElementById('attention');
    box.replaceChildren();
    const items = [
      [a.needs_input, 'need your answers', '#applications?status=needs_input'],
      [a.to_approve, 'waiting for approval', '#applications?status=review'],
      [a.to_check, 'to check (blocked or unconfirmed)', '#applications?status=blocked'],
      [a.unmatched_replies, 'replies not matched to a job', '#applications'],
    ].filter(([n]) => n > 0);
    if (!items.length) {
      box.append(el('a', { class: 'calm', href: '#applications' }, 'Nothing needs you right now'));
      return;
    }
    for (const [n, text, href] of items) box.append(el('a', { href }, el('b', {}, n), text));
  }

  function renderKpis(stats) {
    const t = stats.totals;
    const cards = [
      ['Applied', t.applied, `${stats.window_totals.applied} in the last ${stats.window.days} days`],
      ['Heard back', t.responses, `${pct(t.response_rate)} response rate, ${t.replies_from_a_person} from a person`],
      ['Interviews', t.interviews, `${pct(t.interview_rate)} of applications`],
      ['Offers', t.offers, `${t.rejections} rejections`],
      ['Days to a reply', t.median_days_to_response === null ? '–' : t.median_days_to_response, 'median'],
      ['Jobs found', t.discovered, `${t.queued} queued to apply`],
    ];
    document.getElementById('kpis').replaceChildren(
      ...cards.map(([label, value, note]) =>
        el('div', { class: 'kpi' }, el('div', { class: 'label' }, label),
          el('div', { class: 'value' }, value), el('div', { class: 'note' }, note)))
    );
  }

  let lastSeries = null;
  function renderPerDay(series) {
    lastSeries = series;
    const box = document.getElementById('per-day');
    const W = Math.max(300, Math.round(box.clientWidth || 760));
    const H = W < 500 ? 180 : 220, left = 30, right = 8, top = 10, bottom = 24;
    const max = Math.max(1, ...series.map((d) => Math.max(d.applied, d.responses, d.discovered)));
    const niceMax = niceCeil(max);
    const plotW = W - left - right, plotH = H - top - bottom;
    const slot = plotW / series.length;
    const barW = Math.max(1.5, Math.min(14, slot * 0.28));
    const y = (v) => top + plotH - (v / niceMax) * plotH;
    const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Applications per day' });

    for (let i = 0; i <= 4; i++) {
      const v = (niceMax / 4) * i;
      root.append(svg('line', { class: 'grid-line', x1: left, x2: W - right, y1: y(v), y2: y(v) }));
      root.append(svg('text', { x: left - 6, y: y(v) + 3, 'text-anchor': 'end' }, Math.round(v)));
    }
    const labelEvery = Math.ceil(series.length / Math.max(3, Math.floor(plotW / 70)));
    series.forEach((d, i) => {
      const x = left + i * slot + slot / 2;
      const g = svg('g', {});
      g.append(svg('title', {}, `${shortDate(d.date)}: ${d.applied} applied, ${d.responses} heard back, ${d.discovered} found`));
      g.append(svg('rect', { class: 'bar-discovered', x: x - barW * 1.5, width: barW, y: y(d.discovered), height: Math.max(0, top + plotH - y(d.discovered)), rx: 1.5 }));
      g.append(svg('rect', { class: 'bar-applied', x: x - barW / 2, width: barW, y: y(d.applied), height: Math.max(0, top + plotH - y(d.applied)), rx: 1.5 }));
      g.append(svg('rect', { class: 'bar-responses', x: x + barW / 2, width: barW, y: y(d.responses), height: Math.max(0, top + plotH - y(d.responses)), rx: 1.5 }));
      g.append(svg('rect', { x: left + i * slot, width: slot, y: top, height: plotH, fill: 'transparent' }));
      root.append(g);
      const last = i === series.length - 1;
      if ((i % labelEvery === 0 && series.length - 1 - i >= labelEvery / 2) || last) {
        root.append(svg('text', { x: last ? W - right : x, y: H - 6, 'text-anchor': last ? 'end' : 'middle' }, shortDate(d.date)));
      }
    });
    box.replaceChildren(root);
  }

  // The top of the axis: four whole ticks of 1, 2, 3 or 5 times a power of ten.
  function niceCeil(n) {
    const tick = n / 4;
    const power = Math.pow(10, Math.floor(Math.log10(Math.max(tick, 1))));
    for (const m of [1, 2, 3, 5, 10]) if (m * power >= tick) return 4 * Math.max(1, m * power);
    return 40 * power;
  }

  function renderFunnel(funnel) {
    const top = Math.max(1, funnel[0].count);
    const rows = funnel.map((stage, i) => {
      const prev = i ? funnel[i - 1].count : null;
      const share = prev ? ` · ${Math.round((stage.count / Math.max(prev, 1)) * 100)}% of ${funnel[i - 1].stage.toLowerCase()}` : '';
      return el('div', { class: 'hbar' },
        el('div', { class: 'top-line' }, el('span', {}, stage.stage), el('span', { class: 'num' }, stage.count + share)),
        el('div', { class: 'track' }, el('div', { class: 'fill' + (i >= 3 ? ' second' : i < 2 ? ' found' : ''), style: `width:${(stage.count / top) * 100}%` })));
    });
    document.getElementById('funnel').replaceChildren(el('div', { class: 'hbars' }, rows));
  }

  function renderCategories(rows) {
    const box = document.getElementById('categories');
    if (!rows.length) { box.replaceChildren(el('p', { class: 'sub' }, 'No jobs found yet.')); return; }
    const shown = rows.slice(0, 10);
    const max = Math.max(1, ...shown.map((r) => Math.max(r.applied, r.discovered)));
    const more = rows.length > shown.length ? [el('p', { class: 'sub' }, `and ${rows.length - shown.length} more`)] : [];
    box.replaceChildren(el('div', { class: 'hbars' }, shown.map((r) =>
      el('div', { class: 'hbar' },
        el('div', { class: 'top-line' }, el('span', { title: r.category }, r.category),
          el('span', { class: 'num' }, `${r.applied} applied · ${r.responses} replies · ${r.discovered} found`)),
        el('div', { class: 'track' },
          el('div', { class: 'fill found', style: `width:${(r.discovered / max) * 100}%` }),
          el('div', { class: 'fill', style: `width:${(r.applied / max) * 100}%` }),
          el('div', { class: 'fill second', style: `width:${(r.responses / max) * 100}%` }))))),
      ...more);
  }

  function renderSites(rows) {
    const box = document.getElementById('sites');
    if (!rows.length) { box.replaceChildren(el('p', { class: 'sub' }, 'Nothing sent yet.')); return; }
    box.replaceChildren(el('table', { class: 'table compact' }, el('tbody', {}, rows.map((r) =>
      el('tr', {}, el('td', {}, siteName(r.site)),
        el('td', { class: 'num' }, `${r.applied} applied`),
        el('td', { class: 'num' }, `${r.responses} replies`),
        el('td', { class: 'num' }, pct(r.response_rate)))))));
  }

  function renderActivity(items) {
    const list = document.getElementById('activity');
    if (!items.length) { list.replaceChildren(el('li', { class: 'sub' }, 'Nothing yet.')); return; }
    list.replaceChildren(...items.map((item) => {
      const verb = item.kind === 'applied' ? `Applied via ${siteName(item.site)}`
        : item.kind === 'reply'
          ? (item.moved_to ? `Reply, moved to ${(STATUS_LABEL[item.moved_to] || item.moved_to).toLowerCase()}` : 'Reply received')
          : `Marked ${(STATUS_LABEL[item.detail] || item.detail || '').toLowerCase()}`;
      return el('li', {},
        el('span', { class: 'dot ' + item.kind }),
        el('a', { class: 'what', href: `#applications?id=${item.application_id}`, onclick: () => { state.selectedApp = item.application_id; } },
          `${item.title} · ${item.company}`, el('small', {}, verb)),
        el('span', { class: 'when' }, when(item.at)));
    }));
  }

  document.getElementById('range').addEventListener('click', (event) => {
    const button = event.target.closest('button[data-days]');
    if (!button) return;
    state.days = Number(button.dataset.days);
    for (const b of event.currentTarget.querySelectorAll('button')) b.setAttribute('aria-pressed', String(b === button));
    LOADERS.overview().catch((e) => toast(e.message, true));
  });

  // -------------------------------------------------------- applications --

  LOADERS.applications = async function () {
    const [counts, apps] = await Promise.all([
      api('/api/applications/counts'),
      api('/api/applications' + (state.appStatus ? `?status=${encodeURIComponent(state.appStatus)}` : '')),
    ]);
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    const filters = document.getElementById('app-filters');
    const order = ['needs_input', 'review', 'blocked', 'unconfirmed', 'dry_run', 'applied', 'screening', 'interviewing', 'offer', 'rejected', 'failed', 'withdrawn', 'pending'];
    const chips = [['', 'All', total], ...order.filter((s) => counts[s]).map((s) => [s, STATUS_LABEL[s] || s, counts[s]])];
    filters.replaceChildren(...chips.map(([status, label, n]) =>
      el('button', { type: 'button', 'aria-pressed': String(state.appStatus === status), onclick: () => { state.appStatus = status; location.hash = 'applications' + (status ? '?status=' + status : ''); } },
        label, el('span', { class: 'count' }, n))));

    const body = document.querySelector('#app-table tbody');
    body.replaceChildren(...apps.map((app) => {
      const row = el('tr', { class: 'clickable' + (app.id === state.selectedApp ? ' selected' : ''), 'data-id': app.id, onclick: () => openApp(app.id) },
        el('td', {}, el('div', { class: 'job-title' }, app.job.title), el('div', { class: 'job-meta' }, app.job.company)),
        el('td', {}, siteName(app.ats)),
        el('td', {}, badge(app.status)),
        el('td', {}, app.submitted_at ? when(app.submitted_at) : el('span', { class: 'job-meta' }, 'not sent')));
      return row;
    }));
    document.getElementById('app-empty').hidden = apps.length > 0;
    const wanted = new URLSearchParams(location.hash.split('?')[1] || '').get('id');
    if (wanted) state.selectedApp = Number(wanted);
    if (state.selectedApp) openApp(state.selectedApp).catch(() => closeApp());
    else closeApp();
  };

  function closeApp() {
    state.selectedApp = null;
    document.getElementById('app-detail').hidden = true;
    document.querySelector('#view-applications .split').classList.remove('open');
  }

  async function openApp(id) {
    state.selectedApp = id;
    for (const tr of document.querySelectorAll('#app-table tbody tr')) tr.classList.remove('selected');
    const app = await api(`/api/applications/${id}`);
    const pane = document.getElementById('app-detail');
    const last = app.last_attempt || {};
    const tries = Array.isArray(app.attempts) ? app.attempts.length : app.attempts || 0;
    const parts = [
      el('button', { class: 'close', type: 'button', 'aria-label': 'Close', onclick: closeApp }, '×'),
      el('h3', {}, app.job.title),
      el('div', { class: 'job-meta' }, `${app.job.company} · ${siteName(app.ats)} · `, el('a', { href: app.job.url, target: '_blank', rel: 'noopener noreferrer' }, 'posting')),
      el('p', {}, badge(app.status), ' ', app.submitted_at ? `sent ${when(app.submitted_at)}` : `${tries} attempt${tries === 1 ? '' : 's'}, not sent`),
    ];
    if (last.error) parts.push(el('div', { class: 'error' }, last.error));
    if (last.confirmation) parts.push(el('p', { class: 'sub' }, `The page said: “${last.confirmation}”`));

    const needed = (app.needed || []).filter((n) => n.required);
    if (needed.length) parts.push(questionForm(app, needed));

    if (['dry_run', 'review'].includes(app.status)) {
      parts.push(el('section', {}, el('h4', {}, 'Send it'),
        el('p', { class: 'sub' }, 'The form was filled and left at the submit button. Read the screenshot, then send it.'),
        el('button', { type: 'button', class: 'primary', onclick: (e) => approve(app.id, e.currentTarget) }, 'Submit this application')));
    }

    if (app.submitted_at) {
      const select = el('select', {}, ['screening', 'interviewing', 'offer', 'rejected', 'withdrawn'].map((s) => el('option', { value: s }, STATUS_LABEL[s])));
      parts.push(el('section', {}, el('h4', {}, 'Record what happened'),
        el('div', { class: 'row' }, select,
          el('button', { type: 'button', onclick: (e) => logStatus(app.id, select.value, e.currentTarget) }, 'Save'))));
    }

    if (app.events && app.events.length) {
      parts.push(el('section', {}, el('h4', {}, 'History'), el('ol', { class: 'feed' }, app.events.slice().reverse().map((ev) =>
        el('li', {}, el('span', { class: 'dot ' + (ev.kind === 'email' ? 'reply' : 'status') }),
          el('span', { class: 'what' }, ev.to_status ? STATUS_LABEL[ev.to_status] || ev.to_status : ev.kind === 'email' ? 'Reply received' : 'Note', ev.note ? el('small', {}, ev.note) : null),
          el('span', { class: 'when' }, when(ev.created_at)))))));
    }

    const filled = (last.filled || []);
    if (filled.length) {
      parts.push(el('section', {}, el('h4', {}, `What went on the form (${filled.length})`),
        el('table', { class: 'table compact' }, el('tbody', {}, filled.map((f) =>
          el('tr', {}, el('td', {}, f.label || f.key),
            el('td', {}, f.file_path ? f.file_path.split('/').pop() : Array.isArray(f.value) ? f.value.join(', ') : String(f.value ?? ''),
              f.source === 'prefilled' ? el('span', { class: 'job-meta' }, ' (from your profile)') : null)))))));
    }

    if (app.screenshot_path) {
      parts.push(el('section', {}, el('h4', {}, 'Screenshot'),
        el('a', { href: `/api/applications/${app.id}/screenshot`, target: '_blank' },
          el('img', { class: 'shot', src: `/api/applications/${app.id}/screenshot?t=${Date.now()}`, alt: 'The form as the agent left it', loading: 'lazy' }))));
    }
    pane.replaceChildren(...parts);
    pane.hidden = false;
    document.querySelector('#view-applications .split').classList.add('open');
    for (const tr of document.querySelectorAll('#app-table tbody tr')) {
      tr.classList.toggle('selected', Number(tr.dataset.id) === app.id);
    }
  }

  function questionForm(app, needed) {
    const inputs = needed.map((n) => {
      const key = n.answer_key || n.key;
      const control = n.options && n.options.length
        ? el('select', { name: key, required: true }, el('option', { value: '' }, 'Choose…'), n.options.map((o) => el('option', { value: o }, o)))
        : el(n.kind === 'textarea' ? 'textarea' : 'input', { name: key, required: true, rows: 3 });
      return el('label', { class: 'question' }, el('span', {}, n.label), control, n.reason ? el('small', { class: 'job-meta' }, n.reason) : null);
    });
    const form = el('form', { class: 'stack' }, inputs,
      el('p', { class: 'sub' }, 'Each answer is saved and reused on any form that asks the same thing.'),
      el('button', { type: 'submit' }, 'Save answers and fill again'));
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const answers = {};
      for (const [k, v] of new FormData(form).entries()) if (String(v).trim()) answers[k] = String(v).trim();
      const button = form.querySelector('button[type=submit]');
      button.textContent = 'Filling the form again…';
      try {
        await busy(button, api(`/api/applications/${app.id}/answers`, { method: 'POST', json: { answers, retry: true } }));
        toast('Saved. The form was filled again.');
      } catch (e) {
        toast(e.message, true);
      }
      LOADERS.applications();
    });
    return el('section', {}, el('h4', {}, 'The form asked'), form);
  }

  async function approve(id, button) {
    if (!confirm('Submit this application now? This sends it to the employer.')) return;
    button.textContent = 'Submitting…';
    try {
      const out = await busy(button, api(`/api/applications/${id}/approve`, { method: 'POST' }));
      const outcome = out.result && out.result.outcome;
      toast(outcome === 'submitted' ? 'Sent, and the page confirmed it.' : `Not sent: ${outcome}`, outcome !== 'submitted');
    } catch (e) {
      toast(e.message, true);
    }
    LOADERS.applications();
  }

  async function logStatus(id, status, button) {
    try {
      await busy(button, api(`/api/applications/${id}/events`, { method: 'POST', json: { kind: 'status_change', to_status: status } }));
      toast(`Marked ${STATUS_LABEL[status].toLowerCase()}.`);
      LOADERS.applications();
    } catch (e) {
      toast(e.message, true);
    }
  }

  // ---------------------------------------------------------------- jobs --

  LOADERS.jobs = async function () {
    const minScore = document.getElementById('job-min-score').value;
    const query = new URLSearchParams({ limit: '200' });
    if (state.jobStatus) query.set('status', state.jobStatus);
    if (minScore) query.set('min_score', minScore);
    const [counts, jobs] = await Promise.all([api('/api/jobs/counts'), api('/api/jobs?' + query)]);
    const order = ['queued', 'pending', 'needs_review', 'applied', 'failed', 'skipped'];
    document.getElementById('job-filters').replaceChildren(...order.map((status) =>
      el('button', { type: 'button', 'aria-pressed': String(state.jobStatus === status), onclick: () => { state.jobStatus = status; LOADERS.jobs(); } },
        STATUS_LABEL[status], el('span', { class: 'count' }, counts[status] || 0))));

    document.querySelector('#job-table tbody').replaceChildren(...jobs.map((job) => {
      const actions = [];
      if (job.status !== 'queued' && job.status !== 'applied') actions.push(el('button', { type: 'button', onclick: (e) => setJob(job.id, 'queued', e.currentTarget) }, 'Queue'));
      if (job.status !== 'skipped' && job.status !== 'applied') actions.push(el('button', { type: 'button', onclick: (e) => setJob(job.id, 'skipped', e.currentTarget) }, 'Skip'));
      if (job.status === 'queued') actions.push(el('button', { type: 'button', title: 'Write the resume and cover letter for this job now', onclick: (e) => tailor(job.id, e.currentTarget) }, 'Tailor'));
      return el('tr', {},
        el('td', {}, el('a', { class: 'job-title', href: job.url, target: '_blank', rel: 'noopener noreferrer' }, job.title),
          el('div', { class: 'job-meta' }, [job.company, job.location, siteName(job.ats_type || job.source)].filter(Boolean).join(' · '))),
        el('td', {}, job.category || el('span', { class: 'job-meta' }, '–')),
        el('td', { class: 'num', title: job.score_reason || '' }, job.score ?? '–', job.tier ? el('div', { class: 'job-meta' }, `tier ${job.tier}`) : null),
        el('td', { class: 'job-meta' }, when(job.scraped_at)),
        el('td', {}, el('div', { class: 'row-actions' }, actions)));
    }));
    document.getElementById('job-empty').hidden = jobs.length > 0;
  };

  document.getElementById('job-min-score').addEventListener('change', () => LOADERS.jobs().catch((e) => toast(e.message, true)));

  async function setJob(id, status, button) {
    try {
      await busy(button, api(`/api/jobs/${id}`, { method: 'PATCH', json: { status } }));
      LOADERS.jobs();
    } catch (e) {
      toast(e.message, true);
    }
  }

  async function tailor(id, button) {
    button.textContent = 'Tailoring…';
    try {
      const variant = await busy(button, api(`/api/jobs/${id}/tailor`, { method: 'POST' }));
      toast(variant.status === 'ready' ? 'Resume and letter ready for this job.' : 'Tailoring was rejected by the fact check; see the variant for why.', variant.status !== 'ready');
    } catch (e) {
      toast(e.message, true);
    }
    button.textContent = 'Tailor';
  }

  // ------------------------------------------------------------- profile --

  LOADERS.profile = async function () {
    const [resume, facts, never, answers] = await Promise.all([
      api('/api/resume').catch((e) => (e.status === 404 ? null : Promise.reject(e))),
      api('/api/facts?include_inactive=true'),
      api('/api/never-claim'),
      api('/api/answers'),
    ]);
    document.getElementById('resume-current').textContent = resume
      ? `On file: ${resume.filename || 'resume'}${resume.uploaded_at ? ', uploaded ' + when(resume.uploaded_at) : ''}.`
      : 'No resume uploaded yet.';

    const groups = {};
    for (const fact of facts) (groups[fact.kind] = groups[fact.kind] || []).push(fact);
    document.getElementById('fact-count').textContent = `${facts.filter((f) => f.active).length} active`;
    document.getElementById('facts').replaceChildren(...Object.entries(groups).map(([kind, items]) =>
      el('div', {}, el('h4', {}, kind.replace(/_/g, ' ')), el('ul', {}, items.map((fact) =>
        el('li', { class: fact.active ? '' : 'off' },
          el('span', {}, fact.text, fact.source === 'user_added' ? el('span', { class: 'mine' }, 'added by you') : null),
          el('button', { type: 'button', onclick: (e) => toggleFact(fact, e.currentTarget) }, fact.active ? 'Turn off' : 'Turn on')))))));
    if (!facts.length) document.getElementById('facts').replaceChildren(el('p', { class: 'sub' }, 'Upload a resume to fill the fact base.'));

    document.getElementById('never').replaceChildren(...never.map((row) =>
      el('li', {}, row.term, el('button', { type: 'button', 'aria-label': `Remove ${row.term}`, onclick: () => removeNever(row.term) }, '×'))));

    document.querySelector('#answers tbody').replaceChildren(...(answers.length ? answers.map((a) =>
      el('tr', {}, el('td', {}, a.key.replace(/^q:/, '').replace(/_/g, ' ')), el('td', {}, a.value))) : [el('tr', {}, el('td', {}, 'No answers yet. They are added as forms ask.'))]));
  };

  async function toggleFact(fact, button) {
    try {
      await busy(button, api(`/api/facts/${fact.id}`, { method: 'PATCH', json: { active: !fact.active } }));
      LOADERS.profile();
    } catch (e) { toast(e.message, true); }
  }

  async function removeNever(term) {
    try {
      await api(`/api/never-claim/${encodeURIComponent(term)}`, { method: 'DELETE' });
      LOADERS.profile();
    } catch (e) { toast(e.message, true); }
  }

  document.getElementById('resume-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    try {
      const out = await busy(form.querySelector('button'), api('/api/resume', { method: 'POST', body: new FormData(form) }));
      toast(out.already_uploaded ? 'That resume was already on file.' : `Parsed ${out.filename}: ${out.facts_created} facts added.`);
      form.reset();
      LOADERS.profile();
    } catch (e) { toast(e.message, true); }
  });

  document.getElementById('keywords-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const keywords = splitList(form.keywords.value);
    if (!keywords.length) return;
    try {
      const added = await busy(form.querySelector('button'), api('/api/facts/keywords', { method: 'POST', json: { keywords } }));
      toast(added.length ? `Added ${added.length}.` : 'All of those were already there.');
      form.reset();
      LOADERS.profile();
    } catch (e) { toast(e.message, true); }
  });

  document.getElementById('never-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    try {
      await api('/api/never-claim', { method: 'POST', json: { term: form.term.value } });
      form.reset();
      LOADERS.profile();
    } catch (e) { toast(e.message, true); }
  });

  // ------------------------------------------------------------ settings --

  const JOBSPY_SITES = ['indeed', 'linkedin', 'glassdoor', 'zip_recruiter', 'google'];

  LOADERS.settings = async function () {
    const [sessions, criteria, config] = await Promise.all([
      api('/api/sessions'), api('/api/search-criteria'), loadConfig(),
    ]);
    document.getElementById('sessions').replaceChildren(...sessions.map((s) => {
      const status = s.signed_in
        ? el('span', { class: 'badge good' }, s.expires_at ? `Signed in until ${new Date(s.expires_at).toLocaleDateString()}` : 'Signed in')
        : el('span', { class: 'badge warn' }, s.saved ? 'Expired' : 'Not signed in');
      const action = s.saved
        ? el('button', { type: 'button', class: 'danger', onclick: (e) => forgetSession(s.site, e.currentTarget) }, 'Forget')
        : null;
      return el('div', { class: 'session' },
        el('div', {}, el('b', {}, s.label), ' ', status, el('div', { class: 'job-meta' }, 'Run ', el('code', {}, s.login_command), s.signed_in ? ' again when it expires.' : ' in a terminal on your machine.')),
        action);
    }));

    document.getElementById('config').replaceChildren(
      el('dt', {}, 'Mode'), el('dd', {}, { dry_run: 'Dry run: fill forms, send nothing', review: 'Review: fill, then wait for your approval', auto: 'Auto: submit' }[config.apply_mode] || config.apply_mode),
      el('dt', {}, 'Daily cap'), el('dd', {}, `${config.daily_apply_cap} applications, ${config.easy_apply_daily_cap} each on LinkedIn and Indeed`),
      el('dt', {}, 'Pause between'), el('dd', {}, `about ${Math.round(config.apply_delay_seconds)} seconds`),
      el('dt', {}, 'Model'), el('dd', {}, { 'claude-code': 'Your Claude subscription (claude -p)', api: 'Claude API key', auto: 'Automatic' }[config.llm_backend] || config.llm_backend),
      el('dt', {}, 'Mailbox'), el('dd', {}, config.inbox_configured ? 'Connected (read-only)' : 'Not set up; replies are not tracked'));

    const form = document.getElementById('criteria-form');
    for (const key of ['titles', 'locations', 'keywords', 'exclude_keywords', 'company_blacklist']) form[key].value = (criteria[key] || []).join('\n');
    for (const key of ['min_score', 'max_tier', 'max_age_hours', 'salary_min']) form[key].value = criteria[key] ?? '';
    form.remote_ok.checked = !!criteria.remote_ok;
    form.dataset.original = JSON.stringify(criteria);
    document.getElementById('criteria-sites').replaceChildren(el('span', { class: 'inline' }, 'Search on:'),
      ...JOBSPY_SITES.map((site) => el('label', { class: 'inline check' },
        el('input', { type: 'checkbox', name: 'site', value: site, checked: (criteria.jobspy_sites || []).includes(site) }),
        siteName(site) === site ? site.replace('_', ' ') : siteName(site))));
  };

  async function loadConfig() {
    state.config = await api('/api/config');
    const badgeEl = document.getElementById('mode-badge');
    badgeEl.textContent = { dry_run: 'Dry run', review: 'Review', auto: 'Auto-submit' }[state.config.apply_mode] || state.config.apply_mode;
    badgeEl.classList.toggle('auto', state.config.apply_mode === 'auto');
    return state.config;
  }

  async function forgetSession(site, button) {
    if (!confirm('Delete the saved sign-in? You will need to sign in again before applying there.')) return;
    try {
      await busy(button, api(`/api/sessions/${site}`, { method: 'DELETE' }));
      LOADERS.settings();
    } catch (e) { toast(e.message, true); }
  }

  document.getElementById('criteria-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const body = Object.assign(JSON.parse(form.dataset.original || '{}'), {
      titles: splitList(form.titles.value),
      locations: splitList(form.locations.value),
      keywords: splitList(form.keywords.value),
      exclude_keywords: splitList(form.exclude_keywords.value),
      company_blacklist: splitList(form.company_blacklist.value),
      remote_ok: form.remote_ok.checked,
      jobspy_sites: Array.from(form.querySelectorAll('input[name=site]:checked')).map((i) => i.value),
    });
    for (const key of ['min_score', 'max_tier', 'max_age_hours']) if (form[key].value !== '') body[key] = Number(form[key].value);
    body.salary_min = form.salary_min.value === '' ? null : Number(form.salary_min.value);
    try {
      await busy(form.querySelector('button[type=submit]'), api('/api/search-criteria', { method: 'PUT', json: body }));
      document.getElementById('criteria-note').textContent = 'Saved. The next search uses it.';
      LOADERS.settings();
    } catch (e) { toast(e.message, true); }
  });

  // ------------------------------------------------------------ run buttons --

  const RUNS = {
    discover: { path: '/api/runs/discover', started: 'Searching the boards. New jobs appear under Jobs as they are scored.' },
    apply: { path: '/api/apply', started: null },
    inbox: { path: '/api/inbox/poll', started: 'Reading the mailbox for replies.' },
  };

  document.querySelector('.actions').addEventListener('click', async (event) => {
    const button = event.target.closest('button[data-run]');
    if (!button) return;
    const run = RUNS[button.dataset.run];
    let message = run.started;
    if (button.dataset.run === 'apply') {
      const mode = (state.config || await loadConfig()).apply_mode;
      if (mode === 'auto' && !confirm('The mode is auto: this submits applications. Go ahead?')) return;
      message = mode === 'auto'
        ? 'Applying to queued jobs. Submitted ones appear under Applications.'
        : 'Filling forms for queued jobs; nothing is sent. Results appear under Applications.';
    }
    try {
      await busy(button, api(run.path, { method: 'POST' }));
      toast(message);
      setTimeout(route, 4000);
    } catch (e) {
      toast(e.status === 409 ? 'That is already running.' : e.message, true);
    }
  });

  // ----------------------------------------------------------------- start --

  window.addEventListener('hashchange', route);
  let resizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (lastSeries) renderPerDay(lastSeries); }, 150);
  });
  loadConfig().catch(() => {});
  route();
  setInterval(() => {
    if (document.visibilityState === 'visible' && !document.getElementById('view-overview').hidden) {
      LOADERS.overview().catch(() => {});
    }
  }, 60000);
})();
