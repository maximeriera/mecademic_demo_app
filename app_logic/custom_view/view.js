// Custom view page: reads the hub through DataHubClient and draws on <canvas>.
// No chart library on purpose (no CDN on a show floor); vendor one into this
// folder if a demo needs more.
(function () {
    'use strict';

    const LIVE_WINDOW_S = 15;
    const EVENTS_SHOWN = 10;

    const css = getComputedStyle(document.documentElement);
    const token = (name, fallback) => css.getPropertyValue(name).trim() || fallback;
    const COLORS = {
        x: token('--color-blue', '#3bb8ff'),
        height: token('--color-accent', '#5DEFBF'),
        ng: token('--color-red', '#ff5a6b'),
        nominal: token('--color-accent-2', '#009B72'),
        band: 'rgba(0, 155, 114, 0.18)',
        grid: 'rgba(255, 255, 255, 0.07)',
        label: token('--color-muted', '#9aa39f'),
    };

    let epochMs = null;

    // --- Chart ----------------------------------------------------------------

    function niceTicks(min, max, count) {
        const span = max - min;
        if (!(span > 0)) return { values: [min], step: 1 };
        const raw = span / count;
        const mag = Math.pow(10, Math.floor(Math.log10(raw)));
        const norm = raw / mag;
        const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
        const values = [];
        for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-9; v += step) {
            values.push(+v.toFixed(10));
        }
        return { values, step };
    }

    function decimalsFor(step) {
        return Math.max(0, Math.min(4, -Math.floor(Math.log10(step))));
    }

    function extent(values) {
        let lo = Infinity, hi = -Infinity;
        values.forEach(v => {
            if (v < lo) lo = v;
            if (v > hi) hi = v;
        });
        return [lo, hi];
    }

    /**
     * opts.series: [{points: [[x, y], ...], color}]
     * opts.xRange: [x0, x1] (else auto) · opts.band: {y0, y1} · opts.minYSpan
     */
    function drawChart(canvas, opts) {
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
        ctx.font = '11px Archivo, sans-serif';

        const pad = { l: 48, r: 10, t: 8, b: 22 };
        const plotW = w - pad.l - pad.r;
        const plotH = h - pad.t - pad.b;
        const points = opts.series.flatMap(s => s.points);

        if (!points.length) {
            ctx.fillStyle = COLORS.label;
            ctx.textAlign = 'center';
            ctx.fillText('Waiting for data…', w / 2, h / 2);
            return;
        }

        let [x0, x1] = opts.xRange || extent(points.map(p => p[0]));
        if (!(x1 > x0)) x1 = x0 + 1;
        const ys = points.map(p => p[1]);
        if (opts.band) ys.push(opts.band.y0, opts.band.y1);
        let [y0, y1] = extent(ys);
        const minSpan = opts.minYSpan || 1e-6;
        if (y1 - y0 < minSpan) {
            const mid = (y0 + y1) / 2;
            y0 = mid - minSpan / 2;
            y1 = mid + minSpan / 2;
        }
        const margin = (y1 - y0) * 0.08;
        y0 -= margin;
        y1 += margin;

        const sx = x => pad.l + ((x - x0) / (x1 - x0)) * plotW;
        const sy = y => pad.t + (1 - (y - y0) / (y1 - y0)) * plotH;

        const yTicks = niceTicks(y0, y1, 4);
        const xTicks = niceTicks(x0, x1, 6);
        ctx.strokeStyle = COLORS.grid;
        ctx.lineWidth = 1;
        ctx.fillStyle = COLORS.label;

        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        yTicks.values.forEach(v => {
            const y = Math.round(sy(v)) + 0.5;
            ctx.beginPath();
            ctx.moveTo(pad.l, y);
            ctx.lineTo(w - pad.r, y);
            ctx.stroke();
            ctx.fillText(v.toFixed(decimalsFor(yTicks.step)), pad.l - 6, y);
        });

        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';
        xTicks.values.forEach(v => {
            const x = Math.round(sx(v)) + 0.5;
            ctx.beginPath();
            ctx.moveTo(x, pad.t);
            ctx.lineTo(x, pad.t + plotH);
            ctx.stroke();
            ctx.fillText(v.toFixed(decimalsFor(xTicks.step)), x, pad.t + plotH + 6);
        });

        if (opts.band) {
            ctx.fillStyle = COLORS.band;
            ctx.fillRect(pad.l, sy(opts.band.y1), plotW, sy(opts.band.y0) - sy(opts.band.y1));
            if (opts.band.mid !== undefined) {
                ctx.strokeStyle = COLORS.nominal;
                ctx.setLineDash([4, 4]);
                ctx.beginPath();
                ctx.moveTo(pad.l, sy(opts.band.mid));
                ctx.lineTo(w - pad.r, sy(opts.band.mid));
                ctx.stroke();
                ctx.setLineDash([]);
            }
        }

        ctx.save();
        ctx.beginPath();
        ctx.rect(pad.l, pad.t, plotW, plotH);
        ctx.clip();
        ctx.lineWidth = 2;
        ctx.lineJoin = 'round';
        opts.series.forEach(s => {
            if (!s.points.length) return;
            ctx.strokeStyle = s.color;
            ctx.beginPath();
            s.points.forEach(([x, y], i) => (i ? ctx.lineTo(sx(x), sy(y)) : ctx.moveTo(sx(x), sy(y))));
            ctx.stroke();
        });
        ctx.restore();
    }

    // --- Panels -----------------------------------------------------------------

    function lastCycleDuration(events) {
        for (let i = events.length - 1; i >= 0; i--) {
            const e = events[i];
            if (e.name === 'cycle_end' && e.start_t !== undefined) return e.t - e.start_t;
        }
        return null;
    }

    function renderLive(state) {
        if (state.now === null) return;
        const inCycle = state.cycleStart !== null && state.now - state.cycleStart <= LIVE_WINDOW_S;
        const t0 = inCycle ? state.cycleStart : state.now - LIVE_WINDOW_S;
        // Fixed axis for the cycle (previous cycle's length) so the curve grows
        // left to right instead of the axis rescaling every frame.
        const span = inCycle
            ? Math.max(lastCycleDuration(state.events) || 0, state.now - t0)
            : LIVE_WINDOW_S;
        const rel = name => state.from(name, t0).map(([t, v]) => [t - t0, v]);

        drawChart(document.getElementById('chart-x'), {
            series: [{ points: rel('robot_x'), color: COLORS.x }],
            xRange: [0, span],
            minYSpan: 10,
        });
        drawChart(document.getElementById('chart-height'), {
            series: [{ points: rel('sensor'), color: COLORS.height }],
            xRange: [0, span],
            minYSpan: 1,
        });
    }

    function renderProfile(state) {
        const profile = state.latest('profile');
        const ok = state.latest('profile_ok');
        const caption = document.getElementById('profile-caption');
        if (!profile || !Array.isArray(profile.points)) {
            drawChart(document.getElementById('chart-profile'), { series: [{ points: [], color: COLORS.height }] });
            return;
        }
        const deviation = state.latest('profile_deviation');
        caption.textContent = `— cycle #${profile.cycle}` +
            (deviation !== undefined ? `, max deviation ${deviation.toFixed(2)} mm` : '');
        drawChart(document.getElementById('chart-profile'), {
            series: [{ points: profile.points, color: ok === false ? COLORS.ng : COLORS.height }],
            band: {
                y0: profile.nominal - profile.tolerance,
                y1: profile.nominal + profile.tolerance,
                mid: profile.nominal,
            },
            minYSpan: 1,
        });
    }

    function setBadge(id, channel, state, detailFor) {
        const badge = document.getElementById(id);
        const samples = state.channels[channel] || [];
        if (!samples.length) return;
        const passed = samples[samples.length - 1][1];
        const ng = samples.filter(s => s[1] === false).length;
        badge.classList.toggle('ok', passed === true);
        badge.classList.toggle('ng', passed === false);
        badge.querySelector('.badge-value').textContent = passed ? 'OK' : 'NG';
        badge.querySelector('.badge-detail').textContent =
            (detailFor ? detailFor(state) + ' · ' : '') + `${ng} NG / ${samples.length} recent`;
    }

    function formatEventTime(t) {
        if (epochMs === null) return `${t.toFixed(2)} s`;
        return new Date(epochMs + t * 1000).toLocaleTimeString([], { hour12: false });
    }

    function renderEvents(state) {
        const list = document.getElementById('events');
        const events = state.events.slice(-EVENTS_SHOWN).reverse();
        if (!events.length) return;
        list.replaceChildren(...events.map(e => {
            const li = document.createElement('li');
            const name = document.createElement('span');
            const data = e.data || {};
            name.textContent = e.name +
                (data.cycle !== undefined ? ` #${data.cycle}` : '') +
                (data.error ? ` — ${data.error}` : '');
            const time = document.createElement('span');
            time.className = 't';
            time.textContent = formatEventTime(e.t);
            li.append(name, time);
            return li;
        }));
    }

    function render(state) {
        renderLive(state);
        renderProfile(state);
        setBadge('badge-profile', 'profile_ok', state, s => {
            const d = s.latest('profile_deviation');
            return d !== undefined ? `dev ${d.toFixed(2)} mm` : '';
        });
        setBadge('badge-camera', 'inspection_passed', state);
        renderEvents(state);
    }

    // --- Manifest (hub status) ----------------------------------------------------

    function refreshManifest() {
        fetch('/api/data')
            .then(r => r.json())
            .then(info => {
                if (info.epoch_utc) epochMs = Date.parse(info.epoch_utc);
                const banner = document.getElementById('setup-error');
                banner.hidden = !info.setup_error;
                banner.textContent = info.setup_error ? `Custom view setup failed: ${info.setup_error}` : '';
                const sources = Object.values(info.channels || {}).filter(c => c.rate_hz);
                document.getElementById('view-status').textContent = info.active
                    ? `Live · ${sources.length} source(s) sampled` +
                      sources.map(c => c.errors ? ` · ${c.errors} read error(s)` : '').join('')
                    : 'Hub not running — Initialize the controller.';
            })
            .catch(() => {
                document.getElementById('view-status').textContent = 'Connection lost — retrying…';
            });
    }

    refreshManifest();
    setInterval(refreshManifest, 5000);

    DataHubClient.poll({
        channels: ['robot_x', 'sensor', 'profile', 'profile_ok', 'profile_deviation', 'inspection_passed'],
        intervalMs: 200,
        historyS: 120,
        onData: render,
        onError: () => {
            document.getElementById('view-status').textContent = 'Connection lost — retrying…';
        },
    });
})();
