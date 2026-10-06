/*
 * view-header.js — default top bar for a workspace custom view (/view/):
 * Mecademic logo, system status (state + running task) and the current step,
 * read from /api/status and shown the way the controller's sidebar shows them.
 *
 *   <link rel="stylesheet" href="/static/css/app.css">
 *   <script src="/static/js/view-header.js"></script>
 *
 * Mounts itself as the first element of <body>, or in place of an element with
 * id="view-topbar" if the page has one. Rendering mirrors renderStatusBox() /
 * patchLiveStep() in app.js: keep the two in step.
 */
(function () {
    'use strict';

    const POLL_MS = 100;

    const MARKUP = `
        <a class="view-topbar-logo" href="/" title="Open the controller">
            <img src="/static/img/mecademic-logo-white.svg" alt="Mecademic" width="1868" height="619">
        </a>
        <div class="view-topbar-block">
            <div class="global-status-label">System Status</div>
            <div class="status-box OFF" data-role="status">Loading...</div>
        </div>
        <div class="view-topbar-block">
            <div class="global-status-label">Current Step</div>
            <div class="step-indicator step-indicator-idle" data-role="step">
                <div class="step-indicator-meta">
                    <span class="step-indicator-counter" data-role="counter">Not running</span>
                    <span class="step-indicator-timer" data-role="timer">—</span>
                </div>
                <div class="step-indicator-name" data-role="name">Idle</div>
                <div class="step-progress"><div class="step-progress-fill" data-role="fill"></div></div>
            </div>
        </div>`;

    function formatDuration(value) {
        const seconds = Number(value);
        if (value === null || value === undefined || !Number.isFinite(seconds)) return '—';
        if (seconds < 60) return `${seconds.toFixed(2)} s`;
        const minutes = Math.floor(seconds / 60);
        return `${minutes}m ${(seconds - minutes * 60).toFixed(2).padStart(5, '0')}s`;
    }

    function setText(el, text) {
        if (el.textContent !== text) el.textContent = text;
    }

    function renderStatus(el, status, task, stateClass = status) {
        el.className = 'status-box ' + String(stateClass).toUpperCase();
        const key = `${status}|${task || ''}`;
        if (el.dataset.renderKey === key) return;
        el.dataset.renderKey = key;

        const state = document.createElement('span');
        state.className = 'status-state';
        state.textContent = status;
        el.replaceChildren(state);
        if (task) {
            const taskEl = document.createElement('span');
            taskEl.className = 'status-task';
            taskEl.textContent = task;
            el.appendChild(taskEl);
            el.title = `${status} · ${task}`;
        } else {
            el.removeAttribute('title');
        }
    }

    function renderStep(refs, step) {
        const active = !!step;
        refs.step.classList.toggle('step-indicator-idle', !active);
        refs.step.classList.toggle('step-indicator-active', active);
        if (!active) {
            setText(refs.counter, 'Not running');
            setText(refs.timer, '—');
            setText(refs.name, 'Idle');
            refs.name.removeAttribute('title');
            refs.fill.style.width = '0%';
            return;
        }
        // "Pick part › Close gripper": a sub-step alone loses its context.
        const label = Array.isArray(step.path) && step.path.length > 1 ? step.path.join(' › ') : step.name;
        const hasPosition = step.total && step.index !== null && step.index !== undefined;
        setText(refs.counter, step.off_sequence ? 'Off sequence'
            : hasPosition ? `Step ${step.index} of ${step.total}` : `Step ${step.depth + 1}`);
        setText(refs.timer, formatDuration(step.elapsed_s));
        setText(refs.name, label);
        if (refs.name.title !== label) refs.name.title = label;
        refs.fill.style.width = `${Math.round((step.progress ?? 0) * 100)}%`;
    }

    function poll(refs) {
        fetch('/api/status')
            .then(r => r.json())
            .then(data => {
                renderStatus(refs.status, data.status, data.task || null);
                renderStep(refs, data.step || null);
            })
            .catch(() => {
                renderStatus(refs.status, 'COMMUNICATION ERROR', null, 'FAULTED');
                renderStep(refs, null);
            })
            // Chained rather than setInterval so a slow server never queues requests.
            .finally(() => setTimeout(() => poll(refs), POLL_MS));
    }

    function mount() {
        const header = document.createElement('header');
        header.className = 'view-topbar';
        header.innerHTML = MARKUP;
        const slot = document.getElementById('view-topbar');
        if (slot) {
            slot.replaceWith(header);
        } else {
            document.body.prepend(header);
        }
        const role = name => header.querySelector(`[data-role="${name}"]`);
        poll({
            status: role('status'),
            step: role('step'),
            counter: role('counter'),
            timer: role('timer'),
            name: role('name'),
            fill: role('fill'),
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', mount);
    } else {
        mount();
    }
})();
