// Fiber alignment view: live power against the threshold, a tracker of the
// alignment's phases, and for each phase a plot that fits it: 3D power
// landscapes for the spirals and the gradient circles, 1-D profiles for the
// axial scans, the power over time in dB, and an end-of-alignment summary.
// Reads the hub through DataHubClient, the threshold and the cell state from
// /api/context. Dependency-free canvas drawing (plot3d.js, chart2d.js,
// landscape.js): no CDN on a show floor.
(function () {
    'use strict';

    const THRESHOLD_PARAM = 'power_threshold_mw';
    const CONTEXT_POLL_MS = 1000;
    // A power reading older than this is stale: the hub stopped sampling.
    const STALE_S = 2;
    const EVENTS_SHOWN = 8;
    // Read from a distance on a 27" screen.
    const FONT_PX = 14;
    const AXES_ANIM_MS = 300;
    const SURFACE_REBUILD_MS = 250;
    // Earlier axial scans kept on a profile, fading.
    const PROFILES_SHOWN = 6;
    // Time slices of the power trace, about one per pixel column.
    const TRACE_SLICES = 1000;

    // A sample is [x, y, z, alpha, beta, gamma, power_w, kind, phase, t] (see alignment.py).
    const POWER = 6;
    const KIND = 7;
    const PHASE = 8;
    const T = 9;

    // How each phase of algorithm.PHASES is drawn. Unknown phases get DEFAULT_VIEW.
    const LATERAL = { type: 'map', axes: [0, 2], scale: 1000, names: ['ΔX', 'ΔZ'], unit: 'µm', minSpan: 20 };
    const PHASE_VIEWS = {
        'Cartesian spiral': LATERAL,
        'Angular spiral': { type: 'map', axes: [3, 5], scale: 1, names: ['Δα', 'Δγ'], unit: '°', minSpan: 0.02 },
        'Axial search': { type: 'profiles' },
        'Gradient ascent': { type: 'gradient' },
    };
    const DEFAULT_VIEW = LATERAL;
    const SHORT_NAMES = {
        'Cartesian spiral': 'Cart. spiral',
        'Angular spiral': 'Ang. spiral',
        'Axial search': 'Axial',
        'Gradient ascent': 'Gradient',
    };

    const $ = id => document.getElementById(id);
    const css = getComputedStyle(document.documentElement);
    const token = (name, fallback) => css.getPropertyValue(name).trim() || fallback;
    const ACCENT = token('--color-accent', '#5DEFBF');
    const SOFT = token('--color-accent-soft', '#9DF5D7');
    const MUTED = token('--color-muted', '#9aa39f');
    const YELLOW = token('--color-yellow', '#ffd447');
    const BLUE = token('--color-blue', '#3bb8ff');
    // Power colours of the landscapes: a heat ramp whose lightness rises evenly
    // (OKLCH L 0.40 → 0.96), dim violet → magenta → coral → amber → pale
    // yellow. Each plot spreads it over the dB range it shows, so small
    // differences stay visible, and a colour bar gives the values.
    const HEAT = ['#3f3399', '#6f2fb7', '#a62cb8', '#d83a95', '#f36069', '#fd8b4a', '#feb251', '#fed756', '#fbf6a6'];
    // The ramp reaches at most this far under a plot's top reading, and spans at least this (dB).
    const HEAT_SPAN_DB = 40;
    const HEAT_MIN_SPAN_DB = 0.2;

    const isNum = v => typeof v === 'number' && Number.isFinite(v);
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
    const wrapDeg = a => ((a + 180) % 360 + 360) % 360 - 180;
    const dbOf = (watts, ref) => (watts > 0 && ref > 0 ? 10 * Math.log10(watts / ref) : -60);
    const signed = (v, digits = 1) => `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(digits)}`;

    // HEAT, interpolated into 256 CSS colours.
    const HEAT_LUT = (() => {
        const rgb = HEAT.map(hex => [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16)));
        return Array.from({ length: 256 }, (_, k) => {
            const x = (k / 255) * (rgb.length - 1);
            const i = Math.min(rgb.length - 2, Math.floor(x));
            const c = rgb[i].map((v, j) => Math.round(v + (x - i) * (rgb[i + 1][j] - v)));
            return `rgb(${c[0]}, ${c[1]}, ${c[2]})`;
        });
    })();

    function maxOf(values, fallback = -Infinity) {
        let m = fallback;
        for (const v of values) if (isNum(v) && v > m) m = v;
        return m;
    }

    function minOf(values, fallback = Infinity) {
        let m = fallback;
        for (const v of values) if (isNum(v) && v < m) m = v;
        return m;
    }

    // [low, high] watts a plot's colours span: the dB range of `watts`, at
    // most HEAT_SPAN_DB deep and at least HEAT_MIN_SPAN_DB wide.
    function heatDomain(watts) {
        const hi = maxOf(watts);
        if (!(hi > 0)) return null;
        const lo = Math.max(hi * 10 ** (-HEAT_SPAN_DB / 10), minOf(watts.filter(w => w > 0), hi));
        const missing = HEAT_MIN_SPAN_DB - 10 * Math.log10(hi / lo);
        const pad = missing > 0 ? 10 ** (missing / 20) : 1;
        return [lo / pad, hi * pad];
    }

    // Where `watts` falls on the ramp of `domain`: 0 at its low end, 1 at its top.
    const heatFraction = (domain, watts) => (watts > 0 ? Math.log10(watts / domain[0]) / Math.log10(domain[1] / domain[0]) : 0);
    const heatColor = (domain, watts) => HEAT_LUT[Math.round(clamp(domain ? heatFraction(domain, watts) : 0, 0, 1) * 255)];

    // --- Power units --------------------------------------------------------------

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
        if (!isNum(watts)) return '—';
        const unit = unitFor(watts);
        return `${fmt(watts, unit)} ${unit.name}`;
    }

    // --- Threshold and cell state, from /api/context ---------------------------------

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
        $('n-counts').textContent = cell.aligned_count === undefined ? '—' : `${cell.aligned_count} / ${cell.failed_count}`;
        const last = cell.last_align_power_mw;
        $('last-power').textContent = isNum(last) && last > 0 ? withUnit(last * 1e-3) : '—';
    }

    // --- Live power tile ------------------------------------------------------------------

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
        $('margin').textContent = known && watts > 0 && thresholdW > 0 ? `(${signed(dbOf(watts, thresholdW))} dB)` : '';

        const card = $('power-card');
        card.classList.toggle('above', known && watts >= thresholdW);
        card.classList.toggle('below', known && watts < thresholdW);
        $('power-state').textContent = watts === null ? 'Waiting for the power meter…'
            : thresholdW === null ? `No threshold: add ${THRESHOLD_PARAM} to context.yaml`
            : watts >= thresholdW ? 'Above threshold' : 'Below threshold';
    }

    // --- The alignment being shown ------------------------------------------------------------

    function fromResult(result, t) {
        return {
            key: `result:${t}`,
            status: result.error ? 'error' : 'done',
            fiber: result.fiber,
            threshold_w: result.threshold_w,
            start: result.start,
            timeout_s: result.timeout_s,
            phases: result.phase_names || [],
            samples: result.points || [],
            reads: result.reads,
            phaseLog: (result.phases || []).filter(p => p.t0 !== undefined),
            phaseSummary: result.phases || [],
            profiles: (result.published || {}).profile || [],
            gradient: (result.published || {}).gradient || [],
            elapsed: result.duration_s || 0,
            error: result.error,
            result: result.error ? null : result,
        };
    }

    // The alignment in progress, else the latest one: built from the hub's
    // events and samples, completed by its align_result summary once it ends.
    // The hub keeps only its latest samples, so a page (re)loaded during or
    // after a run lacks the start of it, which the summary holds (decimated).
    function buildRun(state) {
        const events = state.events;
        let start = -1;
        for (let i = events.length - 1; i >= 0; i--) {
            if (events[i].name === 'align_start') { start = i; break; }
        }
        const results = state.channels.align_result;
        const lastResult = results.length ? results[results.length - 1] : null;
        if (start < 0) return lastResult ? fromResult(lastResult[1], lastResult[0]) : null;

        const begin = events[start];
        const after = events.slice(start + 1);
        const end = after.find(e => e.name === 'align_end');
        const t1 = end ? end.t : Infinity;
        const within = ([t]) => t >= begin.t && t <= t1;
        const values = channel => state.channels[channel].filter(within).map(([, v]) => v);

        const phaseLog = [];
        after.forEach(e => {
            if (e.t > t1) return;
            if (e.name === 'align_phase_start') {
                phaseLog.push({ name: e.data.phase, index: e.data.index, t0: e.data.t, status: 'running' });
            } else if (e.name === 'align_phase_end') {
                const open = phaseLog.filter(p => p.name === e.data.phase && p.status === 'running').pop();
                if (open) {
                    Object.assign(open, {
                        t1: e.data.t, status: e.data.status, reads: e.data.reads,
                        best_w: e.data.best_w, best_before_w: e.data.best_before_w,
                    });
                }
            }
        });

        const d = begin.data || {};
        const run = {
            key: `run:${begin.t}`,
            status: end ? (end.data.error ? 'error' : 'done') : 'running',
            fiber: d.fiber,
            threshold_w: d.threshold_w,
            start: d.start,
            timeout_s: d.timeout_s,
            phases: d.phases || [],
            samples: values('align_sample'),
            reads: null,
            phaseLog,
            phaseSummary: null,
            profiles: values('align_profile'),
            gradient: values('align_gradient'),
            elapsed: end ? end.t - begin.t : (state.now || begin.t) - begin.t,
            error: end ? end.data.error : null,
            result: null,
        };
        if (end) {
            // align_result is published right after align_end: until it
            // arrives, the end event's own data stands in for it.
            const summary = lastResult && lastResult[0] >= end.t ? lastResult[1] : null;
            if (summary) {
                run.phaseSummary = summary.phases;
                run.elapsed = summary.duration_s;
                run.reads = summary.reads;
                if (run.samples.length < summary.reads) run.samples = summary.points || [];
                const published = summary.published || {};
                if ((published.profile || []).length > run.profiles.length) run.profiles = published.profile;
                if ((published.gradient || []).length > run.gradient.length) run.gradient = published.gradient;
            }
            if (!end.data.error) run.result = summary || { passed: end.data.passed, power_w: end.data.power_w };
        }
        return run;
    }

    // Each declared phase with its state: pending, running, done, timed out,
    // failed, skipped or not run.
    function phaseStates(run) {
        const names = run.phases.length ? run.phases : [...new Set(run.phaseLog.map(p => p.name))];
        const started = run.phaseLog.map(p => names.indexOf(p.name));
        return names.map((name, i) => {
            const log = run.phaseLog.filter(p => p.name === name).pop();
            if (log) return Object.assign({ name, index: i }, log);
            const summary = (run.phaseSummary || []).find(p => p.name === name);
            if (summary) return Object.assign({ name, index: i }, summary);
            const later = started.some(j => j > i);
            return { name, index: i, status: later ? 'skipped' : run.status === 'running' ? 'pending' : 'not run' };
        });
    }

    function gainDb(phase) {
        return phase.best_before_w > 0 && phase.best_w > 0 ? dbOf(phase.best_w, phase.best_before_w) : null;
    }

    // Readings taken: the summary's count once the run is over, as its points are decimated.
    const readsOf = run => (isNum(run.reads) ? run.reads : run.samples.length);

    // --- Phase tracker and time budget --------------------------------------------------------

    let selected = null;  // phase name or 'summary', chosen by a click once the run is over
    let trackerKey = '';

    function renderTracker(run, states) {
        const running = run.status === 'running';
        const chips = states.map(p => {
            let cls = { running: 'active', done: 'done', 'timed out': 'done', failed: 'failed', skipped: 'skipped' }[p.status] || '';
            let metric = '–';
            if (p.status === 'running') {
                const count = p.name === 'Axial search' ? `scan ${run.profiles.length}`
                    : p.name === 'Gradient ascent' ? `circle ${run.gradient.length}`
                    : `${run.samples.filter(s => s[PHASE] === p.index).length} readings`;
                metric = `${count} · ${(run.elapsed - (p.t0 || 0)).toFixed(1)} s`;
            } else if (p.status === 'done' || p.status === 'timed out' || p.status === 'failed') {
                const gain = gainDb(p);
                const what = gain !== null ? `${signed(gain)} dB` : isNum(p.best_w) ? `found ${withUnit(p.best_w)}` : '';
                metric = `${what}${what ? ' · ' : ''}${((p.t1 || 0) - (p.t0 || 0)).toFixed(1)} s${p.status === 'timed out' ? ' · timed out' : ''}`;
            } else if (p.status === 'skipped') {
                metric = 'skipped';
            }
            const clickable = !running && (p.status === 'done' || p.status === 'timed out' || p.status === 'failed');
            if (clickable) cls += ' clickable';
            if (!running && selected === p.name) cls += ' selected';
            return { key: p.name, cls, name: SHORT_NAMES[p.name] || p.name, metric, clickable };
        });

        // The verdict, as the last chip.
        let vCls = '';
        let vMetric = running ? '–' : run.status === 'error' ? 'interrupted' : '';
        if (run.status === 'done' && run.result) {
            vCls = run.result.passed ? 'pass' : 'fail';
            vMetric = `${run.result.passed ? '✓' : '✗'} ${withUnit(run.result.power_w)}`;
        } else if (run.status === 'error') {
            vCls = 'failed';
        }
        if (!running) vCls += ` clickable${selected === null || selected === 'summary' ? ' selected' : ''}`;
        chips.push({ key: 'summary', cls: vCls, name: 'Verdict', metric: vMetric, clickable: !running });

        const key = JSON.stringify(chips);
        if (key === trackerKey) return;
        trackerKey = key;
        $('tracker').replaceChildren(...chips.map(c => {
            const li = document.createElement('li');
            li.className = `chip ${c.cls}`;
            const name = document.createElement('span');
            name.className = 'chip-name';
            name.textContent = c.name;
            const metric = document.createElement('span');
            metric.className = 'chip-metric';
            metric.textContent = c.metric;
            li.append(name, metric);
            if (c.clickable) {
                li.addEventListener('click', () => {
                    selected = c.key;
                    trackerKey = '';
                    if (lastState) render(lastState);
                });
            }
            return li;
        }));
    }

    function renderBudget(run) {
        const caption = $('run-caption');
        const fiber = run.fiber ? `fiber #${run.fiber}` : 'no fiber held';
        if (run.status === 'running') {
            caption.textContent = `— ${fiber} · aligning…`;
            caption.className = '';
        } else if (run.status === 'error') {
            caption.textContent = `— ${fiber} · interrupted: ${run.error}`;
            caption.className = 'ng';
        } else {
            const r = run.result || {};
            caption.textContent = `— ${fiber} · ${r.passed ? '✓' : '✗'} ${withUnit(r.power_w)} ${r.passed ? '≥' : '<'} ${withUnit(run.threshold_w)}`
                + (r.timed_out ? ' · timed out' : '');
            caption.className = r.passed ? 'ok' : 'ng';
        }
        const timeout = run.timeout_s || 0;
        const frac = timeout ? clamp(run.elapsed / timeout, 0, 1) : 0;
        $('budget-fill').style.width = `${(frac * 100).toFixed(1)}%`;
        $('budget-fill').classList.toggle('warn', frac > 0.8);
        $('budget-text').textContent = timeout ? `${run.elapsed.toFixed(1)} s / ${timeout} s` : `${run.elapsed.toFixed(1)} s`;
    }

    // --- 3D power landscapes ----------------------------------------------------------------------

    // A Plot3D panel showing power over two pose axes: the readings' path, the
    // surface interpolated from them (height = linear power, colour = HEAT
    // over the surface's dB range, with a colour bar), the best reading, and
    // optionally a trail on the floor. Axes fit the data, only grow within
    // one `key`, and animate.
    function landscape(canvas, cam) {
        const plot = Plot3D.create(canvas, cam, { fontPx: FONT_PX });
        let unit = UNITS[1];
        let threshold = null;
        let key = null;
        let ranges = null;
        let built = { key: null, shape: '', at: 0 };
        let domain = null;  // [low, high] watts the colours span
        plot.setColor(z => heatColor(domain, z * unit.scale));

        function extent(values, minSpan) {
            let lo = minOf(values);
            let hi = maxOf(values);
            if (!isNum(lo) || !isNum(hi)) return [-minSpan / 2, minSpan / 2];
            const pad = Math.max((hi - lo) * 0.15, (minSpan - (hi - lo)) / 2, 0);
            return [lo - pad, hi + pad];
        }
        const union = (a, b) => [Math.min(a[0], b[0]), Math.max(a[1], b[1])];

        return {
            plot,
            update(o) {
                const pts = o.points;
                threshold = o.threshold;
                const r = { x: extent(pts.map(p => p[0]), o.minSpan), y: extent(pts.map(p => p[1]), o.minSpan) };
                ranges = key === o.key && ranges ? { x: union(ranges.x, r.x), y: union(ranges.y, r.y) } : r;
                key = o.key;
                const top = Math.max(threshold || 0, maxOf(pts.map(p => p[2]), 0));
                unit = unitFor(top || 1e-3);
                const zTop = ((top || 1e-3) / unit.scale) * 1.1;
                plot.setLabels({ x: `${o.names[0]} (${o.unit})`, y: `${o.names[1]} (${o.unit})`, z: `Power (${unit.name})` });
                plot.setAxes({ x: ranges.x, y: ranges.y, z: [0, zTop] }, AXES_ANIM_MS);
                const pts3 = pts.map(([u, v, w]) => [u, v, w / unit.scale]);
                plot.setPoints(pts3, { tail: o.tail || 150 });

                // Rebuilt at once for a new key, else at most every
                // SURFACE_REBUILD_MS, when the readings or the axes changed.
                const shape = `${ranges.x}|${ranges.y}|${unit.name}|${pts3.length}`;
                const now = performance.now();
                if (o.key !== built.key || (shape !== built.shape && now - built.at > SURFACE_REBUILD_MS)) {
                    const surface = pts3.length >= 3
                        ? Landscape.build(pts3, ranges.x, ranges.y, { n: o.n || 40, sigmaCells: o.sigmaCells || 1.6 })
                        : null;
                    plot.setSurface(surface);
                    // The colours span what the surface shows, else the readings.
                    const shown = surface ? surface.z.flat().filter(isNum) : pts3.map(p => p[2]);
                    domain = heatDomain(shown.map(z => z * unit.scale));
                    plot.setLegend(domain && {
                        stops: HEAT,
                        low: withUnit(domain[0]),
                        high: withUnit(domain[1]),
                        marks: threshold > domain[0] && threshold < domain[1]
                            ? [{ f: heatFraction(domain, threshold), text: 'threshold' }] : [],
                    });
                    built = { key: o.key, shape, at: now };
                }
                let best = null;
                pts3.forEach(p => { if (!best || p[2] > best[2]) best = p; });
                plot.setMarkers(best ? [{ x: best[0], y: best[1], z: best[2], kind: 'best' }] : []);
                plot.setFloorPath(o.floorPath || []);
                plot.setMessage(o.message || 'Waiting for readings…');
            },
        };
    }

    // [u, v, power] of `samples`, as offsets from the start pose along two axes.
    function project(samples, axes, scale, start) {
        const off = (s, i) => (i >= 3 ? wrapDeg(s[i] - start[i]) : s[i] - start[i]);
        return samples.filter(s => isNum(s[POWER])).map(s => [off(s, axes[0]) * scale, off(s, axes[1]) * scale, s[POWER]]);
    }

    const mainCam = Plot3D.camera({ speed: 0.15 });
    const gradCam = Plot3D.camera({ speed: 0.15 });
    const mapView = landscape($('plot-map'), mainCam);
    const summaryView = landscape($('plot-summary'), mainCam);
    const gradLat = landscape($('plot-grad-lat'), gradCam);
    const gradAng = landscape($('plot-grad-ang'), gradCam);

    // --- 2D charts ---------------------------------------------------------------------------------

    const trace = Chart2D.create($('chart-trace'), { fontPx: FONT_PX, xLabel: 'time (s)', yLabel: 'dB vs threshold', message: 'Waiting for an alignment…' });
    const gradientChart = Chart2D.create($('chart-gradient'), { fontPx: FONT_PX - 1, xLabel: 'circle', yLabel: 'relative slope', message: 'No gradient circles.' });
    const AXIAL = ['Rx', 'Rz', 'X', 'Z'];
    const profileCharts = {};
    AXIAL.forEach(axis => {
        profileCharts[axis] = Chart2D.create($(`profile-${axis}`), { fontPx: FONT_PX - 1, message: `Waiting for an ${axis} scan…` });
    });

    Plot3D.animate([mapView.plot, summaryView.plot, trace, gradientChart, ...Object.values(profileCharts)], mainCam);
    Plot3D.animate([gradLat.plot, gradAng.plot], gradCam);

    // The lowest and highest point of each of `buckets` time slices: a long
    // trace drawn fast, with its envelope kept.
    function envelope(points, buckets) {
        if (points.length <= buckets * 2) return points;
        const t0 = points[0][0];
        const span = points[points.length - 1][0] - t0 || 1;
        const out = [];
        let slice = -1, lo = null, hi = null;
        const flush = () => {
            if (!lo) return;
            if (lo === hi) out.push(lo);
            else out.push(...(lo[0] < hi[0] ? [lo, hi] : [hi, lo]));
        };
        points.forEach(p => {
            const k = Math.floor(((p[0] - t0) / span) * buckets);
            if (k !== slice) {
                flush();
                slice = k;
                lo = hi = p;
            } else if (p[1] < lo[1]) {
                lo = p;
            } else if (p[1] > hi[1]) {
                hi = p;
            }
        });
        flush();
        return out;
    }

    function renderTrace(run) {
        const thr = run.threshold_w;
        const points = run.samples.filter(s => isNum(s[POWER]) && isNum(s[T])).map(s => [s[T], dbOf(s[POWER], thr)]);
        // Best so far, as steps.
        const bestLine = [];
        let best = -Infinity;
        points.forEach(([t, db]) => {
            if (db <= best) return;
            if (bestLine.length) bestLine.push([t, best]);
            best = db;
            bestLine.push([t, best]);
        });
        if (points.length) bestLine.push([points[points.length - 1][0], best]);
        const dbs = points.map(p => p[1]);
        const lo = Math.max(-45, Math.min(-10, Math.floor((minOf(dbs, 0) - 2) / 5) * 5));
        const hi = Math.max(5, Math.ceil((maxOf(dbs, 0) + 2) / 5) * 5);
        const tMax = Math.max(10, run.elapsed, maxOf(points.map(p => p[0]), 0)) * 1.05;
        const bands = run.phaseLog.map((p, i) => ({
            x0: p.t0,
            x1: isNum(p.t1) ? p.t1 : run.elapsed,
            label: SHORT_NAMES[p.name] || p.name,
            color: i % 2 ? 'rgba(59, 184, 255, 0.07)' : 'rgba(93, 239, 191, 0.07)',
        }));
        const last = points[points.length - 1];
        trace.setRange({ x: [0, tMax], y: [lo, hi] }, AXES_ANIM_MS);
        trace.setData({
            series: [
                { points: envelope(points, TRACE_SLICES), color: SOFT, width: 1, alpha: 0.55 },
                { points: bestLine, color: ACCENT, width: 2.4 },
            ],
            bands,
            hlines: [{ y: 0, color: ACCENT, dash: [6, 4], label: 'threshold' }],
            markers: last && run.status === 'running' ? [{ x: last[0], y: last[1], color: YELLOW }] : [],
        });
    }

    function renderProfiles(run) {
        AXIAL.forEach(axis => {
            const chart = profileCharts[axis];
            const list = run.profiles.filter(p => p.axis === axis && p.points && p.points.length);
            if (!list.length) {
                chart.setData({});
                return;
            }
            const latest = list[list.length - 1];
            const angular = latest.unit === '°';
            const k = angular ? 1 : 1000;  // mm → µm, degrees stay degrees
            const unitLabel = angular ? '°' : 'µm';
            const shown = list.slice(-PROFILES_SHOWN);
            const top = Math.max(run.threshold_w || 0, maxOf(shown.flatMap(p => p.points.map(q => q[1])), 0)) || 1e-3;
            const unit = unitFor(top);
            const series = shown.map((p, i) => {
                const newest = i === shown.length - 1;
                return {
                    points: p.points.filter(q => isNum(q[1])).map(([s, w]) => [s * k, w / unit.scale]),
                    color: newest ? ACCENT : SOFT,
                    alpha: newest ? 1 : 0.12 + (0.45 * (i + 1)) / shown.length,
                    width: newest ? 2.4 : 1.2,
                    dots: newest,
                };
            });
            const best = latest.points.reduce((a, b) => (isNum(b[1]) && (!a || b[1] > a[1]) ? b : a), null);
            const lim = latest.limit * k;
            chart.setLabels({
                x: `${axis} (${unitLabel}) · scan ${latest.iteration + 1}${latest.rescan ? ' · rescan' : ''}`,
                y: `Power (${unit.name})`,
            });
            chart.setRange({ x: [-2.08 * lim, 1.08 * lim], y: [0, (top / unit.scale) * 1.1] }, AXES_ANIM_MS);
            chart.setData({
                series,
                vlines: best ? [{ x: best[0] * k, color: YELLOW, dash: [4, 4] }] : [],
                hlines: run.threshold_w ? [{ y: run.threshold_w / unit.scale, color: ACCENT, dash: [6, 4], label: 'threshold' }] : [],
            });
        });
    }

    function renderGradient(run, index) {
        const start = run.start;
        const inPhase = run.samples.filter(s => s[PHASE] === index);
        const centres = (kind, axes, scale) => run.gradient
            .filter(g => g.kind === kind && Array.isArray(g.center))
            .map(g => axes.map(i => (i >= 3 ? wrapDeg(g.center[i] - start[i]) : g.center[i] - start[i]) * scale));
        const common = { threshold: run.threshold_w, n: 32, sigmaCells: 2.5, tail: 40 };
        gradLat.update(Object.assign({
            key: `${run.key}:grad-lat`,
            points: project(inPhase.filter(s => s[KIND] === 'cart'), [0, 2], 1000, start),
            names: ['ΔX', 'ΔZ'], unit: 'µm', minSpan: 10,
            floorPath: centres('cart', [0, 2], 1000),
            message: 'Waiting for a lateral circle…',
        }, common));
        gradAng.update(Object.assign({
            key: `${run.key}:grad-ang`,
            points: project(inPhase.filter(s => s[KIND] === 'ang'), [3, 5], 1000, start),
            names: ['Δα', 'Δγ'], unit: 'm°', minSpan: 10,
            floorPath: centres('ang', [3, 5], 1000),
            message: 'Waiting for an angular circle…',
        }, common));
        const lastKind = run.gradient.length ? run.gradient[run.gradient.length - 1].kind : null;
        const running = run.status === 'running';
        // The circle being run next is the other kind than the last one reported.
        const activeKind = running ? (lastKind === 'cart' ? 'ang' : 'cart') : null;
        $('grad-lat-box').classList.toggle('active', activeKind === 'cart');
        $('grad-ang-box').classList.toggle('active', activeKind === 'ang');
    }

    function renderSummary(run, states) {
        summaryView.update({
            key: `${run.key}:summary`,
            points: project(run.samples.filter(s => s[KIND] !== 'ang'), [0, 2], 1000, run.start),
            names: ['ΔX', 'ΔZ'], unit: 'µm', minSpan: 20, threshold: run.threshold_w, n: 44, sigmaCells: 1.8, tail: 1,
        });

        const verdict = $('verdict');
        const r = run.result || {};
        const readings = readsOf(run);
        if (run.status === 'error') {
            verdict.className = 'verdict ng';
            verdict.textContent = 'Alignment interrupted';
            const small = document.createElement('small');
            small.textContent = run.error || '';
            verdict.append(small);
        } else {
            verdict.className = `verdict ${r.passed ? 'ok' : 'ng'}`;
            verdict.textContent = `${r.passed ? '✓ Aligned' : '✗ Below threshold'}: ${withUnit(r.power_w)}`;
            const small = document.createElement('small');
            small.textContent = `threshold ${withUnit(run.threshold_w)} · ${run.elapsed.toFixed(1)} s · ${readings} readings`
                + (r.timed_out ? ' · timed out' : '');
            verdict.append(small);
        }

        const gains = states.map(p => gainDb(p)).filter(isNum);
        const span = Math.max(1, maxOf(gains.map(Math.abs), 1));
        $('waterfall').replaceChildren(...states.map(p => {
            const li = document.createElement('li');
            const gain = gainDb(p);
            li.className = `wf-row${p.status === 'skipped' || p.status === 'not run' ? ' skipped' : ''}${isNum(gain) && gain < 0 ? ' neg' : ''}`;
            const name = document.createElement('span');
            name.textContent = SHORT_NAMES[p.name] || p.name;
            const bar = document.createElement('span');
            bar.className = 'wf-bar';
            const fill = document.createElement('i');
            fill.style.width = isNum(gain) ? `${(Math.abs(gain) / span) * 100}%` : '0';
            bar.append(fill);
            const val = document.createElement('span');
            val.className = 'wf-val';
            val.textContent = isNum(gain) ? `${signed(gain)} dB`
                : isNum(p.best_w) ? withUnit(p.best_w)
                : p.status;
            li.append(name, bar, val);
            return li;
        }));

        // Lateral slopes are in power/mm, angular ones in power/°: each kind
        // is drawn relative to its steepest circle.
        const slopes = kind => {
            const list = run.gradient.map((g, i) => [i + 1, g]).filter(([, g]) => g.kind === kind && isNum(g.magnitude));
            const top = maxOf(list.map(([, g]) => g.magnitude));
            return list.map(([i, g]) => [i, top > 0 ? g.magnitude / top : 0]);
        };
        gradientChart.setRange({ x: [0.5, Math.max(2, run.gradient.length) + 0.5], y: [0, 1.1] }, 0);
        gradientChart.setData({
            series: [
                { points: slopes('cart'), color: ACCENT, width: 2, dots: true },
                { points: slopes('ang'), color: BLUE, width: 2, dots: true },
            ],
        });
    }

    // --- Main panel ------------------------------------------------------------------------------------

    function showPanel(id) {
        ['panel-map', 'panel-profiles', 'panel-gradient', 'panel-summary'].forEach(p => { $(p).hidden = p !== id; });
    }

    function renderMain(run, states) {
        const running = run.status === 'running';
        let phase = null;
        if (running) {
            phase = run.phaseLog.filter(p => p.status === 'running').pop() || null;
        } else if (selected && selected !== 'summary') {
            phase = states.find(p => p.name === selected) || null;
        }

        if (!running && !phase) {
            showPanel('panel-summary');
            $('panel-title').textContent = 'Summary';
            $('panel-sub').textContent = `fiber #${run.fiber || '–'} · ${readsOf(run)} readings · ${run.elapsed.toFixed(1)} s`;
            renderSummary(run, states);
            return;
        }

        const name = phase ? phase.name : 'Starting';
        const view = (phase && PHASE_VIEWS[phase.name]) || DEFAULT_VIEW;
        const index = phase ? phase.index : null;
        const samples = index === null ? run.samples : run.samples.filter(s => s[PHASE] === index);
        const best = maxOf(samples.map(s => s[POWER]));
        const status = running ? 'running' : phase.status;
        const readings = !running && isNum(phase.reads) ? phase.reads : samples.length;
        $('panel-title').textContent = name;
        $('panel-sub').textContent = `${status} · ${readings} readings${isNum(best) ? ` · best ${withUnit(best)}` : ''}`;

        if (view.type === 'profiles') {
            showPanel('panel-profiles');
            renderProfiles(run);
        } else if (view.type === 'gradient') {
            showPanel('panel-gradient');
            renderGradient(run, index);
        } else {
            showPanel('panel-map');
            $('panel-title').textContent = `${name} · power over ${view.names[0]} / ${view.names[1]} (${view.unit})`;
            mapView.update({
                key: `${run.key}:${name}`,
                points: project(samples, view.axes, view.scale, run.start),
                names: view.names, unit: view.unit, minSpan: view.minSpan, threshold: run.threshold_w,
            });
        }
    }

    // --- Live numbers ------------------------------------------------------------------------------------

    function renderNumbers(run) {
        const last = run.samples[run.samples.length - 1];
        const start = run.start;
        const set = (id, text) => { $(id).textContent = text; };
        if (!last || !start) {
            ['n-dx', 'n-dz', 'n-da', 'n-dg', 'n-dy', 'n-db', 'n-best', 'n-gain', 'n-reads'].forEach(id => set(id, '—'));
            return;
        }
        const um = i => `${signed((last[i] - start[i]) * 1000)} µm`;
        const deg = i => `${signed(wrapDeg(last[i] - start[i]), 3)}°`;
        set('n-dx', um(0));
        set('n-dz', um(2));
        set('n-dy', um(1));
        set('n-da', deg(3));
        set('n-dg', deg(5));
        set('n-db', deg(4));
        let best = null;
        let first = null;
        run.samples.forEach(s => {
            if (!isNum(s[POWER])) return;
            if (!first) first = s;
            if (!best || s[POWER] > best[POWER]) best = s;
        });
        set('n-best', best
            ? `${withUnit(best[POWER])} @ ${signed((best[0] - start[0]) * 1000)} / ${signed((best[2] - start[2]) * 1000)} µm`
            : '—');
        set('n-gain', best && first && first[POWER] > 0 ? `${signed(dbOf(best[POWER], first[POWER]))} dB` : '—');
        const tLast = last[T];
        const rate = isNum(tLast) ? run.samples.filter(s => s[T] > tLast - 1).length : 0;
        set('n-reads', run.status === 'running' ? `${run.samples.length} · ${rate}/s` : `${readsOf(run)}`);
    }

    // --- Events and hub status -------------------------------------------------------------------------

    let epochMs = null;

    function describe(event) {
        const data = event.data || {};
        let text = event.name;
        if (data.phase !== undefined) text += ` · ${data.phase}`;
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

    // --- Main loop ------------------------------------------------------------------------------------------

    let lastState = null;
    let runKey = null;

    function render(state) {
        lastState = state;
        renderPower(state);
        renderEvents(state);
        const run = buildRun(state);
        if (!run) return;
        if (run.key !== runKey) {
            runKey = run.key;
            selected = null;
            trackerKey = '';
        }
        const states = phaseStates(run);
        renderTracker(run, states);
        renderBudget(run);
        renderTrace(run);
        renderNumbers(run);
        renderMain(run, states);
    }

    refreshManifest();
    setInterval(refreshManifest, 5000);
    refreshContext();
    setInterval(refreshContext, CONTEXT_POLL_MS);

    DataHubClient.poll({
        channels: ['power', 'align_sample', 'align_result', 'align_profile', 'align_gradient'],
        intervalMs: 100,
        historyS: 1800,
        onData: render,
        onError: () => { $('view-status').textContent = 'Connection lost — retrying…'; },
    });
})();
