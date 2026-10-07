// Custom view page: reads the hub through DataHubClient, draws with Plot3D
// (plot3d.js) and a small 2D chart. No chart library on purpose (no CDN on a
// show floor); vendor one into this folder if a demo needs more.
(function () {
    'use strict';

    const SPARK_WINDOW_S = 10;
    const IDLE_WINDOW_S = 10;
    const EVENTS_SHOWN = 8;

    const $ = id => document.getElementById(id);
    const css = getComputedStyle(document.documentElement);
    const token = (name, fallback) => css.getPropertyValue(name).trim() || fallback;
    const COLORS = {
        power: token('--color-accent', '#5DEFBF'),
        band: 'rgba(0, 155, 114, 0.18)',
        grid: 'rgba(255, 255, 255, 0.07)',
        label: token('--color-muted', '#9aa39f'),
    };

    // Diverging map centred on the mean power; ±1.5 × tolerance saturates.
    // Keep the stops in sync with .colorbar in index.html.
    const STOPS = [
        [-1, [59, 184, 255]],
        [0, [93, 239, 191]],
        [0.6, [255, 212, 71]],
        [1, [255, 90, 107]],
    ];
    let config = null;

    // `value` and `ref` in the same unit. Relative to `ref`, so one scale
    // serves a µW lamp and a W laser alike.
    function powerColor(value, ref) {
        if (!config || !(ref > 0)) return COLORS.power;
        const t = Math.max(-1, Math.min(1, (value - ref) / (1.5 * config.tolerance * ref)));
        let k = 0;
        while (k < STOPS.length - 2 && t > STOPS[k + 1][0]) k++;
        const [ta, ca] = STOPS[k];
        const [tb, cb] = STOPS[k + 1];
        const f = (t - ta) / (tb - ta);
        const c = ca.map((v, i) => Math.round(v + f * (cb[i] - v)));
        return `rgb(${c[0]}, ${c[1]}, ${c[2]})`;
    }

    // --- Power units ------------------------------------------------------------

    // The hub carries watts; the page shows them with an SI prefix picked from
    // the level being looked at.
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

    const pct = fraction => `${(fraction * 100).toFixed(1)} %`;
    const mean = values => values.reduce((sum, v) => sum + v, 0) / values.length;

    // The data's range, widened to at least ±1.5 × tolerance around `ref` so
    // a steady signal does not blow its noise up to full height.
    function zAxis(values, ref) {
        const span = ref > 0 ? [ref * (1 - 1.5 * config.tolerance), ref * (1 + 1.5 * config.tolerance)] : [];
        const all = values.concat(span);
        if (!all.length) return [0, 1];
        let lo = Math.min(...all);
        let hi = Math.max(...all);
        if (!(hi > lo)) {
            const pad = Math.abs(lo) || 1;
            lo -= pad;
            hi += pad;
        }
        return [lo, hi];
    }

    // --- 3D panels --------------------------------------------------------------

    const camera = Plot3D.camera();
    const labels = unit => ({ x: 'X (mm)', y: 'Y (mm)', z: `Power (${unit.name})` });
    const livePlot = Plot3D.create($('plot-live'), camera, { labels: labels(UNITS[1]) });
    const mapPlot = Plot3D.create($('plot-map'), camera, { labels: labels(UNITS[1]) });
    // Each panel colours around its own mean, in its own display unit.
    let liveRef = null;
    let mapRef = null;
    livePlot.setColor(z => powerColor(z, liveRef));
    mapPlot.setColor(z => powerColor(z, mapRef));
    mapPlot.setMessage('Waiting for a complete cycle…');
    Plot3D.animate([livePlot, mapPlot], camera);

    function applyConfig(next) {
        if (config && config.tolerance === next.tolerance
            && String(config.x) === String(next.x) && String(config.y) === String(next.y)) return;
        config = next;
        const sat = 1.5 * config.tolerance;
        $('legend-low').textContent = `≤ −${pct(sat)}`;
        $('legend-high').textContent = `≥ +${pct(sat)}`;
        $('legend-tol').textContent = `· vs the mean; within ± ${pct(config.tolerance)} passes`;
    }

    // The scan to show live: the cycle in progress, else the last finished
    // one (frozen), else the last few seconds before any cycle ran.
    function scanWindow(state) {
        const events = state.events;
        let start = null;
        for (let i = events.length - 1; i >= 0; i--) {
            if (events[i].name === 'cycle_start') { start = events[i]; break; }
        }
        if (!start) return { t0: state.now - IDLE_WINDOW_S, t1: null, label: '— idle, no cycle yet' };
        const end = events.find(e => (e.name === 'cycle_end' || e.name === 'cycle_error') && e.t >= start.t);
        const cycle = start.data && start.data.cycle;
        return end
            ? { t0: start.t, t1: end.t, label: `— cycle #${cycle} (finished)` }
            : { t0: start.t, t1: null, label: `— cycle #${cycle}, scanning…` };
    }

    // What the live readout and the live plot share: the scan's samples, their
    // mean (the reference) and the unit to show them in.
    function liveScan(state) {
        const win = scanWindow(state);
        const samples = state.from('power', win.t0).filter(s => win.t1 === null || s[0] <= win.t1);
        const ref = samples.length ? mean(samples.map(s => s[1])) : null;
        const latest = state.latest('power');
        return { win, samples, ref, unit: unitFor(ref !== null ? ref : latest || 0) };
    }

    function renderLiveScan(state, live) {
        const { win, samples, ref, unit } = live;
        const xy = state.from('robot_xy', win.t0 - 0.2);
        const points = DataHubClient.align(samples, xy).map(([p, q]) => [q[0], q[1], p / unit.scale]);
        liveRef = ref > 0 ? ref / unit.scale : null;
        livePlot.setAxes({ x: config.x, y: config.y, z: zAxis(points.map(p => p[2]), liveRef) });
        livePlot.setLabels(labels(unit));
        livePlot.setPoints(points);
        $('live-caption').textContent = win.label;
    }

    function renderMap(state) {
        const map = state.latest('power_map');
        if (!map) return;
        const unit = unitFor(map.mean);
        const z = map.z.map(row => row.map(v => (v === null ? null : v / unit.scale)));
        mapRef = map.mean > 0 ? map.mean / unit.scale : null;
        mapPlot.setAxes({ x: config.x, y: config.y, z: zAxis(z.flat().filter(v => v !== null), mapRef) });
        mapPlot.setLabels(labels(unit));
        mapPlot.setSurface({ x: map.x, y: map.y, z });
        const deviation = state.latest('power_map_deviation');
        $('map-caption').textContent = `— cycle #${map.cycle} · mean ${fmt(map.mean, unit)} ${unit.name}` +
            (typeof deviation === 'number' ? ` · max deviation ${pct(deviation)}` : ' · no signal');
    }

    // --- Live power readout -------------------------------------------------------

    function drawSpark(canvas, points, band) {
        const dpr = window.devicePixelRatio || 1;
        const w = canvas.clientWidth;
        const h = canvas.clientHeight;
        if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
            canvas.width = Math.round(w * dpr);
            canvas.height = Math.round(h * dpr);
        }
        const ctx = canvas.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, w, h);
        if (!points.length) return;

        const pad = { l: 44, r: 6, t: 6, b: 18 };
        const plotW = w - pad.l - pad.r;
        const plotH = h - pad.t - pad.b;
        const values = points.map(p => p[1]).concat(band ? [band.y0, band.y1] : []);
        let y0 = Math.min(...values);
        let y1 = Math.max(...values);
        const margin = Math.max((y1 - y0) * 0.1, 0.05);
        y0 -= margin;
        y1 += margin;
        const sx = x => pad.l + ((x + SPARK_WINDOW_S) / SPARK_WINDOW_S) * plotW;
        const sy = y => pad.t + (1 - (y - y0) / (y1 - y0)) * plotH;

        ctx.font = '11px Archivo, sans-serif';
        ctx.fillStyle = COLORS.label;
        ctx.strokeStyle = COLORS.grid;
        ctx.lineWidth = 1;
        const ticks = Plot3D.niceTicks(y0, y1, 3);
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        ticks.values.forEach(v => {
            const y = Math.round(sy(v)) + 0.5;
            ctx.beginPath();
            ctx.moveTo(pad.l, y);
            ctx.lineTo(w - pad.r, y);
            ctx.stroke();
            ctx.fillText(v.toFixed(Plot3D.decimalsFor(ticks.step)), pad.l - 6, y);
        });
        ctx.textBaseline = 'top';
        [-10, -5, 0].forEach(s => {
            ctx.textAlign = s ? 'center' : 'right';
            ctx.fillText(s ? `${s} s` : 'now', sx(s), pad.t + plotH + 4);
        });

        if (band) {
            ctx.fillStyle = COLORS.band;
            ctx.fillRect(pad.l, sy(band.y1), plotW, sy(band.y0) - sy(band.y1));
        }
        ctx.strokeStyle = COLORS.power;
        ctx.lineWidth = 1.6;
        ctx.beginPath();
        points.forEach(([x, y], i) => (i ? ctx.lineTo(sx(x), sy(y)) : ctx.moveTo(sx(x), sy(y))));
        ctx.stroke();
    }

    function renderLivePower(state, live) {
        const watts = state.latest('power');
        if (watts === undefined || state.now === null) return;
        const { samples, ref, unit } = live;
        $('power-now').textContent = fmt(watts, unit);
        $('power-unit').textContent = unit.name;

        if (ref > 0) {
            const delta = (watts - ref) / ref;
            $('power-delta').textContent =
                `${delta >= 0 ? '+' : '−'}${pct(Math.abs(delta))} vs mean ${fmt(ref, unit)} ${unit.name}`;
            $('live-card').classList.toggle('out', Math.abs(delta) > config.tolerance);
        } else {
            $('power-delta').textContent = 'No signal — mean power is zero or below';
            $('live-card').classList.remove('out');
        }

        const xy = state.latest('robot_xy');
        if (xy) {
            $('pos-x').textContent = `${xy[0].toFixed(1)} mm`;
            $('pos-y').textContent = `${xy[1].toFixed(1)} mm`;
        }

        if (samples.length) {
            const values = samples.map(s => s[1]);
            $('cycle-min').textContent = `${fmt(Math.min(...values), unit)} ${unit.name}`;
            $('cycle-max').textContent = `${fmt(Math.max(...values), unit)} ${unit.name}`;
        }

        const recent = state.from('power', state.now - SPARK_WINDOW_S)
            .map(([t, v]) => [t - state.now, v / unit.scale]);
        $('spark-unit').textContent = `— power, ${unit.name}`;
        drawSpark($('chart-spark'), recent, ref > 0
            ? { y0: ref * (1 - config.tolerance) / unit.scale, y1: ref * (1 + config.tolerance) / unit.scale }
            : null);
    }

    // --- Results & events ------------------------------------------------------------

    function setResult(id, channel, state, detail) {
        const tile = $(id);
        const samples = state.channels[channel] || [];
        if (!samples.length) return;
        const passed = samples[samples.length - 1][1];
        const ng = samples.filter(s => s[1] === false).length;
        tile.classList.toggle('ok', passed === true);
        tile.classList.toggle('ng', passed === false);
        tile.querySelector('.result-value').textContent = passed ? 'OK' : 'NG';
        tile.querySelector('.result-detail').textContent =
            (detail ? detail + '\n' : '') + `${ng} NG / ${samples.length} recent`;
    }

    let epochMs = null;

    function renderEvents(state) {
        const events = state.events.slice(-EVENTS_SHOWN).reverse();
        if (!events.length) return;
        $('events').replaceChildren(...events.map(e => {
            const li = document.createElement('li');
            const name = document.createElement('span');
            const data = e.data || {};
            name.textContent = e.name +
                (data.cycle !== undefined ? ` #${data.cycle}` : '') +
                (data.error ? ` — ${data.error}` : '');
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
        const next = state.latest('scan_config');
        if (next) applyConfig(next);
        if (config) {
            const live = liveScan(state);
            renderLivePower(state, live);
            renderLiveScan(state, live);
            renderMap(state);
        }
        const deviation = state.latest('power_map_deviation');
        setResult('result-power', 'power_map_ok', state,
            typeof deviation === 'number' ? `max deviation ${pct(deviation)}` : 'no signal');
        setResult('result-camera', 'inspection_passed', state);
        renderEvents(state);
    }

    // --- Hub status -------------------------------------------------------------------

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

    DataHubClient.poll({
        channels: ['robot_xy', 'power', 'scan_config', 'power_map', 'power_map_ok', 'power_map_deviation', 'inspection_passed'],
        intervalMs: 100,
        historyS: 120,
        onData: render,
        onError: () => { $('view-status').textContent = 'Connection lost — retrying…'; },
    });
})();
