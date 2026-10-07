// Fiber alignment view: live power against the threshold, and the power read
// at each robot X/Y during the latest alignment. Reads the hub through
// DataHubClient, the threshold and the cell state from /api/context, and draws
// the graph with Plot3D (plot3d.js). No chart library on purpose (no CDN on a
// show floor); vendor one into this folder if a demo needs more.
(function () {
    'use strict';

    const THRESHOLD_PARAM = 'power_threshold_mw';
    const CONTEXT_POLL_MS = 1000;
    // A power reading older than this is stale: the hub stopped sampling.
    const STALE_S = 2;
    const EVENTS_SHOWN = 8;

    const $ = id => document.getElementById(id);
    const css = getComputedStyle(document.documentElement);
    const token = (name, fallback) => css.getPropertyValue(name).trim() || fallback;
    const ABOVE = token('--color-accent', '#5DEFBF');
    // Points below the threshold run from blue (no light) to amber (almost
    // there). Keep in sync with .colorbar in index.html.
    const LOW = [59, 184, 255];
    const HIGH = [255, 212, 71];

    const isNum = v => typeof v === 'number' && Number.isFinite(v);

    // --- Power units ----------------------------------------------------------------

    // The hub and the alignment carry watts; the page shows them with an SI
    // prefix picked from the value.
    const UNITS = [
        { scale: 1, name: 'W' },
        { scale: 1e-3, name: 'mW' },
        { scale: 1e-6, name: 'µW' },
        { scale: 1e-9, name: 'nW' },
    ];
    const unitFor = watts => UNITS.find(u => Math.abs(watts) >= u.scale) || UNITS[UNITS.length - 1];

    function fmt(watts, unit) {
        const v = watts / unit.scale;
        const a = Math.abs(v);
        return v.toFixed(a >= 100 ? 1 : a >= 10 ? 2 : 3);
    }

    function withUnit(watts) {
        const unit = unitFor(watts);
        return `${fmt(watts, unit)} ${unit.name}`;
    }

    const signed = v => `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(1)}`;

    // --- Threshold and cell state, from /api/context -----------------------------------

    let thresholdW = null;  // null until known (no context file, or no such param)
    let cell = {};          // variable name → value

    function refreshContext() {
        fetch('/api/context')
            .then(r => r.json())
            .then(ctx => {
                const param = ctx.enabled && ctx.params ? ctx.params[THRESHOLD_PARAM] : null;
                thresholdW = param && isNum(param.value) ? param.value * 1e-3 : null;
                cell = {};
                Object.entries(ctx.variables || {}).forEach(([name, entry]) => { cell[name] = entry.value; });
                renderCell();
            })
            .catch(() => { /* the hub poll reports a lost connection */ });
    }

    function renderCell() {
        const held = cell.held_fiber;
        $('held-fiber').textContent = held === undefined ? '—' : held ? `#${held}` : 'none';
        $('next-fiber').textContent = cell.next_fiber === undefined ? '—' : `#${cell.next_fiber}`;
        $('aligned-count').textContent = cell.aligned_count ?? '—';
        $('failed-count').textContent = cell.failed_count ?? '—';
        const last = cell.last_align_power_mw;
        $('last-power').textContent = isNum(last) && last > 0 ? withUnit(last * 1e-3) : '—';
    }

    // --- Live power -----------------------------------------------------------------------

    function renderPower(state) {
        const samples = state.channels.power;
        const last = samples.length ? samples[samples.length - 1] : null;
        const fresh = last && state.now !== null && state.now - last[0] <= STALE_S;
        const watts = fresh && isNum(last[1]) ? last[1] : null;
        const known = watts !== null && thresholdW !== null;

        if (watts !== null) {
            const unit = unitFor(watts);
            $('power-now').textContent = fmt(watts, unit);
            $('power-unit').textContent = unit.name;
        } else {
            $('power-now').textContent = '—';
            $('power-unit').textContent = '';
        }
        $('threshold').textContent = thresholdW !== null ? withUnit(thresholdW) : 'not set';
        $('margin').textContent = known && watts > 0 && thresholdW > 0
            ? `(${signed(10 * Math.log10(watts / thresholdW))} dB)`
            : '';

        const card = $('power-card');
        card.classList.toggle('above', known && watts >= thresholdW);
        card.classList.toggle('below', known && watts < thresholdW);
        $('power-state').textContent = watts === null ? 'Waiting for the power meter…'
            : thresholdW === null ? `No threshold: add ${THRESHOLD_PARAM} to context.yaml`
            : watts >= thresholdW ? 'Above threshold' : 'Below threshold';
    }

    // --- Alignment graph ------------------------------------------------------------------

    const camera = Plot3D.camera();
    const plot = Plot3D.create($('plot'), camera, { labels: { x: 'X (mm)', y: 'Y (mm)', z: 'Power (mW)' } });
    // The drawn alignment's threshold, in the graph's display unit.
    let plotThreshold = null;
    plot.setColor(z => {
        if (!(plotThreshold > 0) || z >= plotThreshold) return ABOVE;
        const f = Math.max(0, Math.min(1, z / plotThreshold));
        const c = LOW.map((v, i) => Math.round(v + f * (HIGH[i] - v)));
        return `rgb(${c[0]}, ${c[1]}, ${c[2]})`;
    });
    plot.setMessage('Waiting for an alignment…');
    Plot3D.animate([plot], camera);

    // The alignment to draw: the one running (its samples since align_start),
    // else the latest one, frozen. Its align_result summary is kept by the hub
    // and by this page however old it is, so the graph survives a page reload.
    function latestAlignment(state) {
        const events = state.events;
        let start = -1;
        for (let i = events.length - 1; i >= 0; i--) {
            if (events[i].name === 'align_start') { start = i; break; }
        }
        const results = state.channels.align_result;
        const lastResult = results.length ? results[results.length - 1] : null;
        if (start < 0) {
            // Ran before the history this page holds: only the summary is left.
            return lastResult ? Object.assign({ status: 'done' }, lastResult[1]) : null;
        }
        const begin = events[start];
        const end = events.slice(start + 1).find(e => e.name === 'align_end');
        const points = state.channels.align_sample
            .filter(([t]) => t >= begin.t && (!end || t <= end.t))
            .map(([, point]) => point);
        const run = Object.assign({}, begin.data, { points });
        if (!end) return Object.assign(run, { status: 'running' });
        if (end.data.error) return Object.assign(run, { status: 'error', error: end.data.error });
        // align_result is published right after align_end: until it arrives,
        // the end event's own data stands in for it.
        const summary = lastResult && lastResult[0] >= end.t ? lastResult[1] : end.data;
        return Object.assign(run, summary, { points, status: 'done' });
    }

    function renderAlignment(state) {
        const run = latestAlignment(state);
        if (!run || !Array.isArray(run.x) || !Array.isArray(run.y)) return;
        const points = (run.points || []).filter(p => Array.isArray(p) && isNum(p[2]));
        const values = points.map(p => p[2]);
        const threshold = isNum(run.threshold_w) ? run.threshold_w : null;
        const top = Math.max(threshold || 0, ...values) || 1e-3;
        const bottom = Math.min(0, ...values);
        const unit = unitFor(top);

        plotThreshold = threshold !== null ? threshold / unit.scale : null;
        plot.setLabels({ x: 'X (mm)', y: 'Y (mm)', z: `Power (${unit.name})` });
        plot.setAxes({ x: run.x, y: run.y, z: [bottom / unit.scale, (top / unit.scale) * 1.1] });
        plot.setPoints(points.map(([x, y, w]) => [x, y, w / unit.scale]));
        plot.setMessage(run.status === 'running' ? 'Alignment started, waiting for the first reading…' : 'No reading recorded.');
        renderCaption(run, values);
    }

    function renderCaption(run, values) {
        const fiber = run.fiber ? `fiber #${run.fiber}` : 'no fiber held';
        let text;
        let cls = '';
        if (run.status === 'running') {
            const best = values.length ? ` · best ${withUnit(Math.max(...values))}` : '';
            text = `— ${fiber} · aligning… ${values.length} reading(s)${best}`;
        } else if (run.status === 'error') {
            text = `— ${fiber} · interrupted: ${run.error}`;
            cls = 'ng';
        } else {
            // Overrange reads +inf, which arrives here as null.
            const power = isNum(run.power_w) ? withUnit(run.power_w) : (run.passed ? 'overrange' : 'no valid reading');
            // The threshold this alignment was judged against: the live one may have changed since.
            const limit = isNum(run.threshold_w) ? withUnit(run.threshold_w) : 'threshold';
            text = `— ${fiber} · ${run.passed ? '✓' : '✗'} ${power} ${run.passed ? '≥' : '<'} ${limit}`;
            if (isNum(run.reads) && isNum(run.duration_s)) text += ` · ${run.reads} readings in ${run.duration_s.toFixed(1)} s`;
            if (run.timed_out) text += ' · timed out';
            cls = run.passed ? 'ok' : 'ng';
        }
        const caption = $('align-caption');
        caption.textContent = text;
        caption.className = cls;
    }

    // --- Events -----------------------------------------------------------------------------

    let epochMs = null;

    function describe(event) {
        const data = event.data || {};
        let text = event.name;
        if (data.cycle !== undefined) text += ` #${data.cycle}`;
        if (data.fiber !== undefined) text += data.fiber ? ` · fiber #${data.fiber}` : ' · no fiber';
        if (data.error) text += ` — ${data.error}`;
        else if (data.passed !== undefined) text += data.passed ? ' · passed' : ' · below threshold';
        return text;
    }

    function renderEvents(state) {
        const events = state.events.slice(-EVENTS_SHOWN).reverse();
        if (!events.length) return;
        $('events').replaceChildren(...events.map(e => {
            const li = document.createElement('li');
            const name = document.createElement('span');
            name.textContent = describe(e);
            const time = document.createElement('span');
            time.className = 't';
            time.textContent = epochMs === null
                ? `${e.t.toFixed(2)} s`
                : new Date(epochMs + e.t * 1000).toLocaleTimeString([], { hour12: false });
            li.append(name, time);
            return li;
        }));
    }

    function render(state) {
        renderPower(state);
        renderAlignment(state);
        renderEvents(state);
    }

    // --- Hub status ---------------------------------------------------------------------------

    function refreshManifest() {
        fetch('/api/data')
            .then(r => r.json())
            .then(info => {
                if (info.epoch_utc) epochMs = Date.parse(info.epoch_utc);
                $('setup-error').hidden = !info.setup_error;
                $('setup-error').textContent = info.setup_error ? `Custom view setup failed: ${info.setup_error}` : '';
                const sources = Object.values(info.channels || {}).filter(c => c.rate_hz);
                const errors = sources.reduce((n, c) => n + (c.errors || 0), 0);
                $('view-status').textContent = info.active
                    ? `Live · ${sources.length} source(s) sampled` + (errors ? ` · ${errors} read error(s)` : '')
                    : 'Hub not running — Initialize the controller.';
            })
            .catch(() => { $('view-status').textContent = 'Connection lost — retrying…'; });
    }

    refreshManifest();
    setInterval(refreshManifest, 5000);
    refreshContext();
    setInterval(refreshContext, CONTEXT_POLL_MS);

    DataHubClient.poll({
        channels: ['power', 'align_sample', 'align_result'],
        intervalMs: 100,
        historyS: 1800,
        onData: render,
        onError: () => { $('view-status').textContent = 'Connection lost — retrying…'; },
    });
})();
