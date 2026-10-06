/*
 * datahub.js — polling client for a workspace custom view (served on /view/).
 *
 *   <script src="/static/js/datahub.js"></script>
 *   DataHubClient.poll({
 *       channels: ['robot_x', 'sensor', 'profile'],   // 'events' is always added
 *       intervalMs: 200,
 *       historyS: 60,                                   // local retention, seconds
 *       onData(state) { render(state); },
 *   });
 *
 * state.now              hub clock (seconds)
 * state.channels[name]   accumulated [[t, value], ...], oldest first
 * state.events           [{name, t, data, start_t?}, ...]
 * state.cycleStart       t of the latest cycle_start, or null
 * state.latest(name)     last value of a channel, or undefined
 * state.from(name, t0)   samples with t >= t0, e.g. state.from('sensor', state.cycleStart)
 *
 * DataHubClient.align(ref, other) mirrors DataHub.align in Python: pairs each
 * ref value with `other` (numbers or numeric arrays such as [x, y]) linearly
 * interpolated at the same t → [[refValue, otherValue], ...].
 */
(function (global) {
    'use strict';

    function lerp(a, b, f) {
        return Array.isArray(a) ? a.map((x, i) => x + f * (b[i] - x)) : a + f * (b - a);
    }

    function align(ref, other) {
        const pairs = [];
        const n = other.length;
        if (!ref.length || !n) return pairs;
        const first = other[0][0];
        const last = other[n - 1][0];
        let j = 0;
        for (const [t, value] of ref) {
            if (t < first || t > last) continue;
            while (j + 1 < n && other[j + 1][0] < t) j++;
            const [ta, va] = other[j];
            if (j + 1 < n && other[j + 1][0] > ta) {
                const [tb, vb] = other[j + 1];
                pairs.push([value, lerp(va, vb, (t - ta) / (tb - ta))]);
            } else {
                pairs.push([value, va]);
            }
        }
        return pairs;
    }

    function poll(options) {
        const intervalMs = options.intervalMs || 250;
        const historyS = options.historyS || 60;
        const onData = options.onData || function () {};
        const onError = options.onError || function () {};
        const names = Array.from(new Set((options.channels || []).concat(['events'])));

        const channels = {};
        names.forEach(name => { channels[name] = []; });

        const state = {
            now: null,
            channels,
            cycleStart: null,
            get events() {
                return channels.events.map(sample => sample[1]);
            },
            latest(name) {
                const samples = channels[name];
                return samples && samples.length ? samples[samples.length - 1][1] : undefined;
            },
            from(name, t0) {
                const samples = channels[name] || [];
                return (t0 === null || t0 === undefined) ? samples.slice() : samples.filter(s => s[0] >= t0);
            },
        };

        let cursor = null;
        let stopped = false;
        let timer = null;

        function reset() {
            cursor = null;
            state.cycleStart = null;
            names.forEach(name => { channels[name].length = 0; });
        }

        // Keep at least the newest sample so latest() survives sparse channels.
        function trim(samples, cutoff) {
            let drop = 0;
            while (drop < samples.length - 1 && samples[drop][0] < cutoff) drop++;
            if (drop) samples.splice(0, drop);
        }

        function ingest(payload) {
            // A cursor going backwards means the server restarted: start over.
            if (cursor !== null && payload.cursor !== null && payload.cursor < cursor) reset();
            cursor = payload.cursor;
            state.now = payload.now;
            const data = payload.data || {};
            names.forEach(name => {
                const fresh = data[name] || [];
                const target = channels[name];
                for (let i = 0; i < fresh.length; i++) target.push(fresh[i]);
                if (name === 'events') {
                    fresh.forEach(sample => {
                        if (sample[1] && sample[1].name === 'cycle_start') state.cycleStart = sample[0];
                    });
                }
            });
            if (state.now !== null) {
                const cutoff = state.now - historyS;
                names.forEach(name => trim(channels[name], cutoff));
            }
        }

        function tick() {
            if (stopped) return;
            const params = new URLSearchParams();
            names.forEach(name => params.append('ch', name));
            if (cursor !== null) params.set('after', cursor);
            fetch('/api/data/read?' + params.toString())
                .then(r => {
                    if (!r.ok) throw new Error('HTTP ' + r.status);
                    return r.json();
                })
                .then(payload => {
                    ingest(payload);
                    onData(state);
                })
                .catch(onError)
                .finally(() => {
                    if (!stopped) timer = setTimeout(tick, intervalMs);
                });
        }

        tick();
        return {
            state,
            stop() {
                stopped = true;
                clearTimeout(timer);
            },
        };
    }

    global.DataHubClient = { poll, align };
})(window);
