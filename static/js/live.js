/* Live updates over server-sent events (/api/events).
 *
 * One stream per page replaces the 10 Hz /api/status and 1 Hz /api/info,
 * /api/prod/metrics and /api/context polls. app.js registers a handler per
 * event and keeps its polling as a fallback: every poller skips its fetch
 * while Live.active is true, so polling resumes by itself whenever the
 * stream is down (server restarting, proxy dropping it) and stops again once
 * EventSource has reconnected.
 */
const Live = (() => {
    const EVENTS = ['status', 'devices', 'prod', 'context', 'notice', 'workspace'];
    const handlers = {};
    let source = null;
    let active = false;

    function on(event, handler) {
        handlers[event] = handler;
    }

    function dispatch(event, raw) {
        const handler = handlers[event];
        if (!handler) return;
        let data;
        try {
            data = JSON.parse(raw);
        } catch (err) {
            console.warn(`Live: bad ${event} payload`, err);
            return;
        }
        try {
            handler(data);
        } catch (err) {
            console.error(`Live: ${event} handler failed`, err);
        }
    }

    function connect() {
        if (!window.EventSource) return;   // very old browser: polling only
        source = new EventSource('/api/events');
        source.onopen = () => { active = true; };
        source.onerror = () => {
            active = false;
            // EventSource retries by itself after a dropped stream, but gives up
            // for good on a non-stream answer (e.g. 503 while shutting down).
            if (source.readyState === EventSource.CLOSED) {
                setTimeout(connect, 5000);
            }
        };
        EVENTS.forEach((name) => {
            source.addEventListener(name, (event) => {
                active = true;
                dispatch(name, event.data);
            });
        });
    }

    return {
        on,
        connect,
        get active() { return active; },
    };
})();
