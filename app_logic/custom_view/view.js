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
        height: token('--color-accent', '#5DEFBF'),
        band: 'rgba(0, 155, 114, 0.18)',
        grid: 'rgba(255, 255, 255, 0.07)',
        label: token('--color-muted', '#9aa39f'),
    };

    // Diverging map centred on nominal; ±1.5 × tolerance saturates.
    // Keep the stops in sync with .colorbar in index.html.
    const STOPS = [
        [-1, [59, 184, 255]],
        [0, [93, 239, 191]],
        [0.6, [255, 212, 71]],
        [1, [255, 90, 107]],
    ];
    let config = null;

    function heightColor(z) {
        if (!config) return COLORS.height;
        const t = Math.max(-1, Math.min(1, (z - config.nominal) / (1.5 * config.tolerance)));
        let k = 0;
        while (k < STOPS.length - 2 && t > STOPS[k + 1][0]) k++;
        const [ta, ca] = STOPS[k];
        const [tb, cb] = STOPS[k + 1];
        const f = (t - ta) / (tb - ta);
        const c = ca.map((v, i) => Math.round(v + f * (cb[i] - v)));
        return `rgb(${c[0]}, ${c[1]}, ${c[2]})`;
    }

    // --- 3D panels --------------------------------------------------------------

    const camera = Plot3D.camera();
    const labels = { x: 'X (mm)', y: 'Y (mm)', z: 'Height (mm)' };
    const livePlot = Plot3D.create($('plot-live'), camera, { labels });
    const surfacePlot = Plot3D.create($('plot-surface'), camera, { labels });
    livePlot.setColor(heightColor);
    surfacePlot.setColor(heightColor);
    surfacePlot.setMessage('Waiting for a complete cycle…');
    Plot3D.animate([livePlot, surfacePlot], camera);

    function applyConfig(next) {
        if (config && config.nominal === next.nominal && config.tolerance === next.tolerance
            && String(config.x) === String(next.x) && String(config.y) === String(next.y)) return;
        config = next;
        const span = 3 * config.tolerance;
        const axes = { x: config.x, y: config.y, z: [config.nominal - span, config.nominal + span] };
        livePlot.setAxes(axes);
        surfacePlot.setAxes(axes);
        const sat = 1.5 * config.tolerance;
        $('legend-low').textContent = `≤ ${(config.nominal - sat).toFixed(2)} mm`;
        $('legend-high').textContent = `≥ ${(config.nominal + sat).toFixed(2)} mm`;
        $('legend-tol').textContent = `· nominal ${config.nominal.toFixed(2)} ± ${config.tolerance.toFixed(2)} mm`;
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

    function renderLiveScan(state) {
        const win = scanWindow(state);
        const inWindow = s => win.t1 === null || s[0] <= win.t1;
        const heights = state.from('sensor', win.t0).filter(inWindow);
        const xy = state.from('robot_xy', win.t0 - 0.2);
        const points = DataHubClient.align(heights, xy).map(([h, p]) => [p[0], p[1], h]);
        livePlot.setPoints(points);
        $('live-caption').textContent = win.label;
    }

    function renderSurface(state) {
        const surface = state.latest('surface');
        if (!surface) return;
        surfacePlot.setSurface(surface);
        const deviation = state.latest('surface_deviation');
        $('surface-caption').textContent = `— cycle #${surface.cycle}` +
            (deviation !== undefined ? `, max deviation ${deviation.toFixed(2)} mm` : '');
    }

    // --- Live height readout ------------------------------------------------------

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
        ctx.strokeStyle = COLORS.height;
        ctx.lineWidth = 1.6;
        ctx.beginPath();
        points.forEach(([x, y], i) => (i ? ctx.lineTo(sx(x), sy(y)) : ctx.moveTo(sx(x), sy(y))));
        ctx.stroke();
    }

    function renderLiveHeight(state) {
        const height = state.latest('sensor');
        if (height === undefined || state.now === null) return;
        $('height-now').textContent = height.toFixed(2);

        if (config) {
            const delta = height - config.nominal;
            $('height-delta').textContent =
                `${delta >= 0 ? '+' : '−'}${Math.abs(delta).toFixed(2)} mm vs nominal ${config.nominal.toFixed(2)}`;
            $('live-card').classList.toggle('out', Math.abs(delta) > config.tolerance);
        }

        const xy = state.latest('robot_xy');
        if (xy) {
            $('pos-x').textContent = `${xy[0].toFixed(1)} mm`;
            $('pos-y').textContent = `${xy[1].toFixed(1)} mm`;
        }

        const win = scanWindow(state);
        const cycle = state.from('sensor', win.t0).filter(s => win.t1 === null || s[0] <= win.t1).map(s => s[1]);
        if (cycle.length) {
            $('cycle-min').textContent = `${Math.min(...cycle).toFixed(2)} mm`;
            $('cycle-max').textContent = `${Math.max(...cycle).toFixed(2)} mm`;
        }

        const recent = state.from('sensor', state.now - SPARK_WINDOW_S).map(([t, v]) => [t - state.now, v]);
        drawSpark($('chart-spark'), recent,
            config ? { y0: config.nominal - config.tolerance, y1: config.nominal + config.tolerance } : null);
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
        renderLiveHeight(state);
        renderLiveScan(state);
        renderSurface(state);
        const deviation = state.latest('surface_deviation');
        setResult('result-surface', 'surface_ok', state,
            deviation !== undefined ? `max deviation ${deviation.toFixed(2)} mm` : '');
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
        channels: ['robot_xy', 'sensor', 'scan_config', 'surface', 'surface_ok', 'surface_deviation', 'inspection_passed'],
        intervalMs: 100,
        historyS: 120,
        onData: render,
        onError: () => { $('view-status').textContent = 'Connection lost — retrying…'; },
    });
})();
