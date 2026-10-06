const stateDisplays = () => Array.from(document.querySelectorAll('.js-robot-state'));
const messageLog   = document.getElementById('message-log');
let latestContext = null;
let logDropdownInitialized = false;
let backupRestoreDropdownsInitialized = false;
let backupRestoreRobots = [];
let restoreArchiveFile = null;
let manualActions = [];
let latestControllerStatus = 'OFF';

function normalizeStatus(status) {
    return String(status || '').trim().toUpperCase();
}

/* ---- Tab switching ---- */
function switchTab(name, btn) {
    document.querySelectorAll('.tab-panel').forEach(panel => {
        panel.classList.remove('active');
        panel.hidden = true;
    });
    document.querySelectorAll('.tab-btn').forEach(tabButton => {
        tabButton.classList.remove('active');
        tabButton.setAttribute('aria-selected', 'false');
    });

    const activePanel = document.getElementById('tab-' + name);
    activePanel.classList.add('active');
    activePanel.hidden = false;
    // The frame no longer resizes per tab, so start each tab at the top.
    document.querySelector('.main-content').scrollTop = 0;
    btn.classList.add('active');
    btn.setAttribute('aria-selected', 'true');
    if (name === 'logs') populateLogFileList();
    if (name === 'production') {
        refreshProdMetrics();
    }
    if (name === 'variables') {
        refreshContext();
    }
    if (name === 'backup-restore') {
        loadBackupRestoreRobots();
    }
    if (name === 'manual') {
        loadManualActions();
    }
}

/* ---- Status polling ---- */
function updateRobotStatus() {
    fetch('/api/status')
        .then(r => r.json())
        .then(data => {
            latestControllerStatus = data.status;
            patchLiveStep(data.step || null);
            stateDisplays().forEach((el) => renderStatusBox(el, data.status, data.task || null));
            updateManualActionAvailability(data.status);
        })
        .catch(() => {
            latestControllerStatus = 'FAULTED';
            // Without this the indicator freezes on a stale step while the
            // status box reads COMMUNICATION ERROR.
            patchLiveStep(null);
            stateDisplays().forEach((el) => renderStatusBox(el, 'COMMUNICATION ERROR', null, 'FAULTED'));
            updateManualActionAvailability('FAULTED');
        });
}

// State plus, while a task runs, its name ("Busy" / "Production"). Layout is
// CSS's job: a second line in the large box, "Busy · Production" on one line
// in the compact sidebar box. Runs at 10 Hz, so the children are only rebuilt
// when the text changes; className is rewritten every time, as before.
function renderStatusBox(el, status, task, stateClass = status) {
    const compact = el.classList.contains('status-box-compact');
    el.className = 'status-box ' + (compact ? 'status-box-compact ' : '') + 'js-robot-state ' + String(stateClass).toUpperCase();

    const renderKey = `${status}|${task || ''}`;
    if (el.dataset.renderKey === renderKey) return;
    el.dataset.renderKey = renderKey;

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

function setMessage(text) { messageLog.textContent = text || 'No recent command activity.'; }

function setProdActionMessage(text, isError = false) {
    const actionLog = document.getElementById('prod-action-log');
    if (!actionLog) return;
    actionLog.textContent = text || 'No recent production action.';
    actionLog.className = 'message-log ' + (isError ? 'prod-locked' : 'prod-unlocked');
}

function setContextMessage(text, isError = false) {
    const actionLog = document.getElementById('ctx-action-log');
    if (!actionLog) return;
    actionLog.textContent = text || 'No recent context change.';
    actionLog.className = 'message-log ' + (isError ? 'prod-locked' : 'prod-unlocked');
}

function setManualActionMessage(text, isError = false) {
    const actionLog = document.getElementById('manual-action-log');
    if (!actionLog) return;
    actionLog.textContent = text || 'No recent manual action activity.';
    actionLog.className = 'message-log ' + (isError ? 'prod-locked' : 'prod-unlocked');
}

function setBackupRestoreMessage(text, isError = false) {
    const log = document.getElementById('backup-restore-action-log');
    if (!log) return;
    log.textContent = text || 'No backup/restore command activity.';
    log.className = 'message-log ' + (isError ? 'prod-locked' : 'prod-unlocked');
}

/* ---- Task / control commands ---- */
function sendTask(task) {
    fetch(`/api/task/${task}`, { method: 'POST' })
        .then(r => r.json()).then(d => {
            setMessage(d.message);
            setProdActionMessage(d.message, !d.success);
        })
        .catch(() => {
            setMessage('Error sending task command.');
            setProdActionMessage('Error sending task command.', true);
        });
}
function sendStop() {
    fetch('/api/stop', { method: 'POST' })
        .then(r => r.json()).then(d => {
            setMessage(d.message);
            setProdActionMessage(d.message, !d.success);
        })
        .catch(() => {
            setMessage('Error sending stop command.');
            setProdActionMessage('Error sending stop command.', true);
        });
}
function sendAbort() {
    showConfirmModal(
        "Confirm System Abort",
        "Are you sure you want to abort the system? This will immediately stop all ongoing processes.",
        () => {
            fetch('/api/abort', { method: 'POST' })
                .then(r => r.json()).then(d => {
                    setMessage(d.message);
                    setProdActionMessage(d.message, !d.success);
                    setManualActionMessage(d.message, !d.success);
                })
                .catch(() => {
                    setMessage('Error sending abort command.');
                    setProdActionMessage('Error sending abort command.', true);
                    setManualActionMessage('Error sending abort command.', true);
                });
        }
    );
}
function sendInitialize() {
    fetch('/api/initialize', { method: 'POST' })
        .then(r => r.json()).then(d => setMessage(d.message))
        .catch(() => setMessage('Error sending initialization command.'));
}
function sendClearFaults() {
    fetch('/api/clear_faults', { method: 'POST' })
        .then(r => r.json()).then(d => setMessage(d.message))
        .catch(() => setMessage('Error sending clear faults command.'));
}
function showConfirmModal(title, message, onConfirm) {
    const modal = document.getElementById('confirm-modal');
    const titleEl = document.getElementById('modal-title');
    const messageEl = document.getElementById('modal-message');
    const confirmBtn = document.getElementById('modal-confirm');
    const cancelBtn = document.getElementById('modal-cancel');

    titleEl.textContent = title;
    messageEl.textContent = message;
    modal.hidden = false;

    const cleanup = () => {
        modal.hidden = true;
        confirmBtn.removeEventListener('click', handleConfirm);
        cancelBtn.removeEventListener('click', handleCancel);
    };

    const handleConfirm = () => {
        onConfirm();
        cleanup();
    };

    const handleCancel = () => {
        cleanup();
    };

    confirmBtn.addEventListener('click', handleConfirm);
    cancelBtn.addEventListener('click', handleCancel);
}

function sendShutdown() {
    showConfirmModal(
        "Confirm System Shutdown",
        "Are you sure you want to shut down the system? This will disconnect all devices and stop all ongoing processes.",
        () => {
            fetch('/api/shutdown', { method: 'POST' })
                .then(r => r.json()).then(d => setMessage(d.message))
                .catch(() => setMessage('Error sending shutdown command.'));
        }
    );
}

/* ---- Manual runtime actions ---- */

// Built-in tasks shown on the Manual tab for every project, ahead of the
// project-specific actions from config.yaml. They run through /api/task/<key>.
const DEFAULT_MANUAL_ACTIONS = [
    {
        key: 'home',
        label: 'Home',
        description: 'Move the cell to its home position.',
        confirm_title: 'Confirm Home',
        confirm_message: 'Run the HOME task now? Ensure the cell is clear before starting.',
        task: 'home',
    },
    {
        key: 'shipment',
        label: 'Shipment',
        description: 'Move the robots to their shipment/packing position.',
        confirm_title: 'Confirm Shipment',
        confirm_message: 'Run the SHIPMENT task now? Ensure the cell is clear before starting.',
        task: 'shipment',
    },
    {
        key: 'calibration',
        label: 'Calibration',
        description: 'Run the cell calibration routine.',
        confirm_title: 'Confirm Calibration',
        confirm_message: 'Run the CALIBRATION task now? Ensure the cell is clear before starting.',
        task: 'calibration',
    },
];

function loadManualActions() {
    fetch('/api/manual/actions')
        .then(r => r.json().then((data) => ({ ok: r.ok, data })))
        .then(({ ok, data }) => {
            if (!ok) {
                throw new Error(data.message || 'Failed to load manual actions.');
            }
            manualActions = Array.isArray(data.actions) ? data.actions : [];
            renderManualActions();
        })
        .catch((err) => {
            manualActions = [];
            renderManualActions(err.message || 'Failed to load manual actions.');
            setManualActionMessage(err.message || 'Failed to load manual actions.', true);
        });
}

function renderManualActions(loadError = null) {
    const container = document.getElementById('manual-actions-container');
    if (!container) return;

    const configuredActions = Array.isArray(manualActions) ? manualActions : [];

    container.innerHTML = '';
    const grid = document.createElement('div');
    grid.className = 'manual-actions-grid';

    [...DEFAULT_MANUAL_ACTIONS, ...configuredActions].forEach((action) => {
        const card = document.createElement('div');
        card.className = 'manual-action-card';

        const title = document.createElement('div');
        title.className = 'manual-action-title';
        title.textContent = action.label || action.key || 'Unnamed action';
        card.appendChild(title);

        if (action.description) {
            const description = document.createElement('div');
            description.className = 'manual-action-description';
            description.textContent = action.description;
            card.appendChild(description);
        }

        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn-task manual-action-run-btn';
        button.textContent = `RUN ${String(action.label || action.key || 'ACTION').toUpperCase()}`;
        button.dataset.actionKey = action.key || '';
        button.dataset.actionLabel = action.label || action.key || 'Action';
        button.dataset.confirmTitle = action.confirm_title || `Confirm ${action.label || action.key || 'Action'}`;
        button.dataset.confirmMessage = action.confirm_message || `Run manual action '${action.label || action.key || 'Action'}'? Ensure the cell is clear before proceeding.`;
        if (action.task) button.dataset.task = action.task;
        button.addEventListener('click', () => runManualAction(button));
        card.appendChild(button);

        grid.appendChild(card);
    });

    container.appendChild(grid);

    if (loadError) {
        const error = document.createElement('p');
        error.className = 'empty-state empty-state-error';
        error.textContent = loadError;
        container.appendChild(error);
    }

    updateManualActionAvailability(latestControllerStatus);
}

function updateManualActionAvailability(status) {
    const buttons = document.querySelectorAll('.manual-action-run-btn');
    const normalizedStatus = normalizeStatus(status);
    const canRun = normalizedStatus === 'READY';
    const hint = document.getElementById('manual-state-hint');

    if (hint) {
        if (canRun) {
            hint.textContent = 'Ready to run';
            hint.className = 'manual-state-hint manual-state-ready';
        } else {
            hint.textContent = `Locked until READY (${status})`;
            hint.className = 'manual-state-hint manual-state-blocked';
        }
    }

    buttons.forEach((btn) => {
        btn.disabled = !canRun;
        if (!canRun) {
            btn.title = `Manual actions can only run when system state is READY (current: ${status}).`;
        } else {
            btn.title = '';
        }
    });
}

function runManualAction(buttonEl) {
    const actionKey = buttonEl?.dataset?.actionKey || '';
    const actionLabel = buttonEl?.dataset?.actionLabel || actionKey;
    const confirmTitle = buttonEl?.dataset?.confirmTitle || `Confirm ${actionLabel}`;
    const confirmMessage = buttonEl?.dataset?.confirmMessage || `Run manual action '${actionLabel}'? Ensure the cell is clear before proceeding.`;

    if (!actionKey) {
        setManualActionMessage('Manual action key is missing.', true);
        return;
    }

    if (normalizeStatus(latestControllerStatus) !== 'READY') {
        const msg = `Manual action '${actionLabel}' not started. Controller is ${latestControllerStatus}; initialize first.`;
        setMessage(msg);
        setManualActionMessage(msg, true);
        return;
    }

    const task = buttonEl?.dataset?.task || '';
    const url = task
        ? `/api/task/${encodeURIComponent(task)}`
        : `/api/manual/actions/${encodeURIComponent(actionKey)}/run`;

    showConfirmModal(confirmTitle, confirmMessage, () => {
        fetch(url, { method: 'POST' })
            .then(r => r.json().then((data) => ({ ok: r.ok, data })))
            .then(({ ok, data }) => {
                const msg = data.message || `Manual action '${actionLabel}' requested.`;
                const isError = !ok || data.success === false;
                setMessage(msg);
                setProdActionMessage(msg, isError);
                setManualActionMessage(msg, isError);
            })
            .catch(() => {
                const msg = `Error starting manual action '${actionLabel}'.`;
                setMessage(msg);
                setProdActionMessage(msg, true);
                setManualActionMessage(msg, true);
            });
    });
}

function sendManualAbort() {
    showConfirmModal(
        'Confirm Manual Abort',
        'Abort the current running task immediately? This can interrupt robot motion and skip cleanup moves.',
        () => {
            fetch('/api/abort', { method: 'POST' })
                .then(r => r.json())
                .then((d) => {
                    setMessage(d.message);
                    setProdActionMessage(d.message, !d.success);
                    setManualActionMessage(d.message, !d.success);
                })
                .catch(() => {
                    setMessage('Error sending abort command.');
                    setProdActionMessage('Error sending abort command.', true);
                    setManualActionMessage('Error sending abort command.', true);
                });
        }
    );
}

/* ---- Shared rendering helpers ---- */
// Escapes quotes as well as angle brackets. Without the quote cases, any value
// interpolated into an HTML attribute (e.g. a str param's value="...") could
// close the attribute and inject new ones — a str param set to
// `x" onfocus=alert(1) autofocus x="` was enough to run script on render.
function escapeHtml(text) {
    return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function parseFieldValue(typeName, inputEl) {
    if (typeName === 'bool') return !!inputEl.checked;
    const raw = inputEl.value;
    if (typeName === 'int') return parseInt(raw, 10);
    if (typeName === 'float') return parseFloat(raw);
    return raw;
}

// innerHTML re-renders at 1 Hz can briefly shrink the page; keep the reader
// where they were. Depending on the layout either the panel or .main-content
// is the scroller, so both are saved.
function captureScrollState(panelId) {
    const panel = document.getElementById(panelId);
    const main = document.querySelector('.main-content');
    return { panel: panel ? panel.scrollTop : 0, main: main ? main.scrollTop : 0 };
}

function restoreScrollState(panelId, state) {
    const panel = document.getElementById(panelId);
    const main = document.querySelector('.main-content');
    if (panel) panel.scrollTop = state.panel || 0;
    if (main) main.scrollTop = state.main || 0;
}

function formatDurationSeconds(value) {
    if (value === null || value === undefined || Number.isNaN(value)) return '—';
    const seconds = Number(value);
    if (!Number.isFinite(seconds)) return '—';
    if (seconds < 60) return `${seconds.toFixed(2)} s`;
    const minutes = Math.floor(seconds / 60);
    const remainder = (seconds - (minutes * 60)).toFixed(2).padStart(5, '0');
    return `${minutes}m ${remainder}s`;
}

function formatTimestamp(value) {
    if (!value) return '—';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return '—';
    return parsed.toLocaleString();
}

function renderMetricCard(label, value, tone = '', valueId = '') {
    // valueId is optional and lets a card's value be patched in place at 10 Hz
    // (see patchLiveStep) instead of waiting for the next 1 Hz re-render.
    const idAttr = valueId ? ` id="${escapeHtml(valueId)}"` : '';
    return `
        <div class="prod-metric-card ${tone}">
            <div class="prod-metric-label">${escapeHtml(label)}</div>
            <div class="prod-metric-value"${idAttr}>${escapeHtml(String(value))}</div>
        </div>
    `;
}

function renderRecentCycles(history) {
    if (!Array.isArray(history) || history.length === 0) {
        return '<p class="prod-metric-empty">No completed cycles yet.</p>';
    }

    const rows = history.slice(-5).reverse().map((cycle) => {
        const title = cycle.cycle_number !== undefined ? `Cycle ${cycle.cycle_number}` : 'Cycle';
        const duration = formatDurationSeconds(cycle.duration_s);
        const startedAt = formatTimestamp(cycle.started_at);
        const endedAt = formatTimestamp(cycle.ended_at);
        return `
            <li>
                <strong>${escapeHtml(title)}</strong>
                <span>${escapeHtml(duration)}</span>
                <span>${escapeHtml(startedAt)} → ${escapeHtml(endedAt)}</span>
            </li>
        `;
    }).join('');

    return `<ul class="prod-metric-list">${rows}</ul>`;
}

function renderRecentRuns(history) {
    if (!Array.isArray(history) || history.length === 0) {
        return '<p class="prod-metric-empty">No archived runs yet.</p>';
    }

    const rows = history.slice(-3).reverse().map((run, index) => {
        const label = run.end_event ? `${run.end_event} run` : `Run ${history.length - index}`;
        const cycles = run.cycle_count ?? 0;
        const elapsed = formatDurationSeconds(run.elapsed_production_s);
        const endedAt = formatTimestamp(run.run_end);
        const startedAt = formatTimestamp(run.run_start);
        return `
            <li>
                <strong>${escapeHtml(label)}</strong>
                <span>${escapeHtml(cycles)} cycles · ${escapeHtml(elapsed)}</span>
                <span>${escapeHtml(startedAt)} → ${escapeHtml(endedAt)}</span>
            </li>
        `;
    }).join('');

    return `<ul class="prod-metric-list">${rows}</ul>`;
}

/* ---- Production tab ---- */
// Step timings and recent cycle/run history (.prod-detail) start hidden. The
// open state is a class on the static #prod-metrics-panel wrapper rather than
// on the containers renderProdMetrics/renderProdSteps rebuild at 1 Hz, so it
// survives every re-render without being tracked here.
function toggleProdDetails() {
    const panel = document.getElementById('prod-metrics-panel');
    const button = document.getElementById('prod-details-toggle');
    if (!panel || !button) return;
    const open = panel.classList.toggle('prod-details-open');
    button.setAttribute('aria-expanded', String(open));
    button.textContent = open ? 'Hide Details' : 'Show Details';
}

function renderProdMetrics(data) {
    const metrics = data.metrics || {};
    const recentCycles = metrics.cycle_history || [];
    const recentRuns = metrics.run_history || [];
    const metricsContainer = document.getElementById('prod-metrics-container');
    if (!metricsContainer) return;

    const cards = [
        renderMetricCard('Operational State', metrics.running ? 'RUNNING' : 'IDLE', metrics.running ? 'prod-metric-on' : 'prod-metric-off'),
        renderMetricCard('Completed Cycles', metrics.completed_cycles ?? 0),
        renderMetricCard('Current Run Time', formatDurationSeconds(metrics.elapsed_production_s)),
        renderMetricCard('Last Cycle Duration', formatDurationSeconds(metrics.last_cycle_duration_s)),
        renderMetricCard('Average Cycle Time', formatDurationSeconds(metrics.average_cycle_duration_s)),
        renderMetricCard('Cumulative Time', formatDurationSeconds(metrics.total_cycle_time_s)),
        renderMetricCard('Last Error State', metrics.last_error || 'NO ERRORS', metrics.last_error ? 'prod-metric-warn' : ''),
    ].join('');

    metricsContainer.innerHTML = `
        <div class="prod-metrics-grid">${cards}</div>
        <div class="prod-metric-history prod-detail">
            <div class="prod-metric-history-title">Recent Cycles</div>
            ${renderRecentCycles(recentCycles)}
        </div>
        <div class="prod-metric-history prod-detail">
            <div class="prod-metric-history-title">Recent Runs</div>
            ${renderRecentRuns(recentRuns)}
        </div>
    `;
}

/* ---- Sequence steps ----
 * Two writers on the same DOM, split by cadence:
 *   renderProdSteps(data)  - 1 Hz, from renderProdSnapshot, writes structure via innerHTML
 *   patchLiveStep(step)    - 10 Hz, from updateRobotStatus, touches ONLY textContent
 *                            and style.width on the ids below.
 * innerHTML at 10 Hz would destroy focus and reset the reader's scroll.
 * Both writers must agree on these ids, so they live in one place. */
const STEP_LIVE_IDS = {
    panelName: 'prod-step-name',
    panelElapsed: 'prod-step-elapsed',
    panelCounter: 'prod-step-counter',
    panelFill: 'prod-step-fill',
    cardElapsed: 'prod-step-card-elapsed',
    cardPosition: 'prod-step-card-position',
};

// Live indicators (sidebar, Control tab). Each is an element with this id plus
// -name / -timer / -counter / -fill children, so one patch serves both. Both
// are compact: they name only the top-level step (no "› sub-step"), timed from
// when that step began. The Production panel keeps the full path.
const STEP_INDICATORS = [
    { id: 'step-indicator', compact: true },
    { id: 'control-step-indicator', compact: true },
];

let lastAnnouncedStep = null;

function setTextById(id, text) {
    const el = document.getElementById(id);
    if (el && el.textContent !== text) el.textContent = text;
}

function announceStep(text) {
    const el = document.getElementById('step-announcer');
    if (el) el.textContent = text;
}

// full / compact: { name, timer, counter } for the full and compact indicators.
function patchStepIndicators(active, full, compact, pct) {
    for (const { id, compact: isCompact } of STEP_INDICATORS) {
        const view = isCompact ? compact : full;
        const indicator = document.getElementById(id);
        if (indicator) {
            indicator.classList.toggle('step-indicator-idle', !active);
            indicator.classList.toggle('step-indicator-active', active);
        }
        setTextById(`${id}-name`, view.name);
        setTextById(`${id}-timer`, view.timer);
        setTextById(`${id}-counter`, view.counter);
        const nameEl = document.getElementById(`${id}-name`);
        // The compact name is truncated to one line; the tooltip keeps the full path.
        if (isCompact && nameEl && nameEl.title !== full.name) nameEl.title = full.name;
        const fill = document.getElementById(`${id}-fill`);
        if (fill) fill.style.width = pct;
    }
}

// "Pick part › Close gripper": the innermost step alone loses its context, so
// a sub-step is shown after its parent(s) on the same line.
function stepLabel(step) {
    return Array.isArray(step.path) && step.path.length > 1
        ? step.path.join(' › ')
        : step.name;
}

function patchLiveStep(step) {
    if (!step) {
        // Switch to an idle state rather than unmounting. The indicators must
        // not change shape between running and idle: an element that appears
        // and disappears moves everything below it and makes the operator hunt
        // for the information.
        const idle = { name: 'Idle', timer: '—', counter: 'Not running' };
        patchStepIndicators(false, idle, idle, '0%');

        setTextById(STEP_LIVE_IDS.panelName, 'Idle');
        setTextById(STEP_LIVE_IDS.panelElapsed, '—');
        setTextById(STEP_LIVE_IDS.panelCounter, 'Not running');
        setTextById(STEP_LIVE_IDS.cardElapsed, '—');
        setTextById(STEP_LIVE_IDS.cardPosition, '—');
        patchSequenceHighlight(null);
        const panelFill = document.getElementById(STEP_LIVE_IDS.panelFill);
        if (panelFill) panelFill.style.width = '0%';
        if (lastAnnouncedStep !== null) {
            announceStep('Idle');
            lastAnnouncedStep = null;
        }
        return;
    }

    const label = stepLabel(step);
    const elapsed = formatDurationSeconds(step.elapsed_s);
    const hasPosition = step.total && step.index !== null && step.index !== undefined;
    const counter = hasPosition
        ? `Step ${step.index} of ${step.total}`
        : `Step ${step.depth + 1}`;
    const pct = `${Math.round((step.progress ?? 0) * 100)}%`;

    const rootName = Array.isArray(step.path) && step.path.length > 0 ? step.path[0] : step.name;
    patchStepIndicators(true,
        { name: label, timer: elapsed, counter: step.off_sequence ? 'Off sequence' : counter },
        {
            name: rootName,
            timer: formatDurationSeconds(step.root_elapsed_s ?? step.elapsed_s),
            // Depth-based "Step 2" means nothing once sub-steps are hidden.
            counter: step.off_sequence ? 'Off sequence' : (hasPosition ? counter : 'Running'),
        },
        pct);

    // Mirror into the Production panel when it is rendered.
    setTextById(STEP_LIVE_IDS.panelName, label);
    setTextById(STEP_LIVE_IDS.panelElapsed, elapsed);
    setTextById(STEP_LIVE_IDS.panelCounter, step.off_sequence ? 'Off sequence' : counter);
    const panelFill = document.getElementById(STEP_LIVE_IDS.panelFill);
    if (panelFill) panelFill.style.width = pct;

    // The cards duplicate the live row, so they must move on the same tick -
    // otherwise the 1 Hz card and the 10 Hz row show different steps at once.
    setTextById(STEP_LIVE_IDS.cardElapsed, elapsed);
    setTextById(STEP_LIVE_IDS.cardPosition,
        hasPosition ? `${step.index} / ${step.total}` : `Step ${step.depth + 1}`);

    patchSequenceHighlight(hasPosition ? step.index - 1 : null);

    // Announce only on an actual step change - a live region that updates
    // 10x/sec would make a screen reader unusable.
    if (label !== lastAnnouncedStep) {
        announceStep(counter ? `${label}, ${counter}` : label);
        lastAnnouncedStep = label;
    }
}

function patchSequenceHighlight(currentIndex) {
    // Class-only update on a stable list: the declared steps do not change
    // within a run, so the highlight can follow the 10 Hz live step without
    // re-rendering (and without fighting the 1 Hz structural render).
    const items = document.querySelectorAll('#prod-steps-container .step-seq-item');
    if (items.length === 0) return;
    items.forEach((li) => {
        const index = Number(li.dataset.seqIndex);
        let cls = 'step-seq-upcoming';
        let marker = '';
        if (currentIndex === null) {
            cls = 'step-seq-upcoming';
        } else if (index < currentIndex) {
            cls = 'step-seq-done'; marker = '\u2713';
        } else if (index === currentIndex) {
            cls = 'step-seq-current'; marker = '\u25b6';
        }
        if (!li.classList.contains(cls)) {
            li.classList.remove('step-seq-done', 'step-seq-current', 'step-seq-upcoming');
            li.classList.add(cls);
        }
        const markerEl = li.querySelector('.step-seq-marker');
        if (markerEl && markerEl.textContent !== marker) markerEl.textContent = marker;
    });
}

function renderStepSequence(sequence, live) {
    if (!sequence || !Array.isArray(sequence.declared) || sequence.declared.length === 0) {
        return '';
    }
    const currentName = live ? live.name : null;
    const items = sequence.declared.map((name, i) => {
        let cls = 'step-seq-upcoming';
        if (i < sequence.current_index) cls = 'step-seq-done';
        else if (i === sequence.current_index && live) cls = 'step-seq-current';
        else if (i === sequence.current_index) cls = 'step-seq-done';
        const marker = cls === 'step-seq-done' ? '✓' : (cls === 'step-seq-current' ? '▶' : '');
        return `<li class="step-seq-item ${cls}" data-seq-index="${i}">
                    <span class="step-seq-marker" aria-hidden="true">${marker}</span>
                    <span class="step-seq-index">${i + 1}</span>
                    <span class="step-seq-name">${escapeHtml(name)}</span>
                </li>`;
    }).join('');

    const offBadge = sequence.off_sequence
        ? '<span class="step-off-sequence">Off sequence</span>'
        : '';
    return `
        <div class="prod-metric-history">
            <div class="prod-metric-history-title">Declared Sequence ${offBadge}</div>
            <ol class="step-seq-list">${items}</ol>
        </div>
    `;
}

function renderStepStats(stats) {
    const rows = (stats || []).filter(s => (s.count || 0) > 0 || (s.fail_count || 0) > 0);
    if (rows.length === 0) {
        return '<p class="prod-metric-empty">No completed steps yet.</p>';
    }
    const items = rows.slice(0, 12).map((s) => {
        const share = (s.share !== null && s.share !== undefined)
            ? ` · ${Math.round(s.share * 100)}% of cycle` : '';
        const fails = s.fail_count
            ? ` · <span class="step-stat-fail">${s.fail_count} failed</span>` : '';
        const indent = s.depth > 0 ? ' step-stat-nested' : '';
        return `
            <li class="${indent.trim()}">
                <strong>${escapeHtml(s.name)}</strong>
                <span>avg ${escapeHtml(formatDurationSeconds(s.avg_s))} ·
                      last ${escapeHtml(formatDurationSeconds(s.last_s))} ·
                      min ${escapeHtml(formatDurationSeconds(s.min_s))} /
                      max ${escapeHtml(formatDurationSeconds(s.max_s))}</span>
                <span>${s.count} run(s)${share}${fails}</span>
            </li>
        `;
    }).join('');
    return `<ul class="prod-metric-list">${items}</ul>`;
}

function renderProdSteps(data) {
    const container = document.getElementById('prod-steps-container');
    if (!container) return;

    const steps = data.steps || {};
    const live = steps.live || null;
    const metrics = data.metrics || {};

    const cards = [
        renderMetricCard('Step Elapsed', formatDurationSeconds(live ? live.elapsed_s : null),
            '', STEP_LIVE_IDS.cardElapsed),
        renderMetricCard('Sequence Position',
            live
                ? ((live.total && live.index !== null && live.index !== undefined)
                    ? `${live.index} / ${live.total}`
                    : `Step ${live.depth + 1}`)
                : '—',
            live && live.off_sequence ? 'prod-metric-warn' : '',
            STEP_LIVE_IDS.cardPosition),
        // metrics.current_cycle_elapsed_s has been served on every poll since
        // the context was introduced and was never displayed until now.
        renderMetricCard('Cycle Elapsed', formatDurationSeconds(metrics.current_cycle_elapsed_s)),
    ].join('');

    // The live row carries the ids patchLiveStep writes at 10 Hz.
    const liveRow = `
        <div class="step-live-row">
            <div class="step-live-head">
                <span id="${STEP_LIVE_IDS.panelCounter}" class="step-indicator-counter"></span>
                <span id="${STEP_LIVE_IDS.panelElapsed}" class="step-indicator-timer">—</span>
            </div>
            <div id="${STEP_LIVE_IDS.panelName}" class="step-live-name">${escapeHtml(live ? stepLabel(live) : 'Idle')}</div>
            <div class="step-progress">
                <div id="${STEP_LIVE_IDS.panelFill}" class="step-progress-fill"></div>
            </div>
        </div>
    `;

    container.innerHTML = `
        ${liveRow}
        <div class="prod-metrics-grid">${cards}</div>
        ${renderStepSequence(steps.sequence, live)}
        <div class="prod-metric-history prod-detail">
            <div class="prod-metric-history-title">Step Timings${steps.stats_truncated ? ' (truncated)' : ''}</div>
            ${renderStepStats(steps.stats)}
        </div>
    `;

    // Re-apply live values immediately: the structure was just replaced, so the
    // next 10 Hz tick would otherwise leave stale text for up to 100 ms.
    patchLiveStep(live);
}

function renderProdUnavailable(message) {
    const metricsContainer = document.getElementById('prod-metrics-container');
    if (!metricsContainer) return;
    metricsContainer.innerHTML = `
        <div class="empty-panel">
            <div class="empty-panel-title">Production metrics unavailable</div>
            <div class="empty-panel-text">${escapeHtml(message || 'Unable to load production metrics right now.')}</div>
        </div>
    `;
}

function exportProdRunHistory() {
    fetch('/api/prod/runs')
        .then(r => r.json().then(data => ({ ok: r.ok, data })))
        .then(({ ok, data }) => {
            if (!ok) {
                const msg = data.message || 'Failed to export production run history.';
                setMessage(msg);
                setProdActionMessage(msg, true);
                return;
            }

            const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const anchor = document.createElement('a');
            anchor.href = url;
            anchor.download = 'production_run_history.json';
            document.body.appendChild(anchor);
            anchor.click();
            anchor.remove();
            URL.revokeObjectURL(url);

            const msg = `Exported ${data.count || 0} archived run(s).`;
            setMessage(msg);
            setProdActionMessage(msg, false);
        })
        .catch(() => {
            const msg = 'Error exporting production run history.';
            setMessage(msg);
            setProdActionMessage(msg, true);
        });
}

let prodMetricsLoaded = false;

function renderProdSnapshot(data) {
    const scrollState = captureScrollState('tab-production');
    prodMetricsLoaded = true;
    renderProdMetrics(data);
    renderProdSteps(data);
    restoreScrollState('tab-production', scrollState);
}

function refreshProdMetrics() {
    fetch('/api/prod/metrics')
        .then(r => r.json().then(data => ({ ok: r.ok, data })))
        .then(({ ok, data }) => {
            // Without the ok check a 500 body ({message: ...}) rendered as an
            // empty snapshot, blanking the metrics and step panels.
            if (!ok) throw new Error(data.message || 'Failed to load production metrics.');
            renderProdSnapshot(data);
        })
        .catch(() => {
            if (!prodMetricsLoaded) {
                renderProdUnavailable('Failed to load production metrics.');
            }
            setProdActionMessage('Production metrics are temporarily unavailable.', true);
        });
}

/* ---- Variables tab (optional context: params & variables) ---- */
function isContextUserInteracting() {
    const active = document.activeElement;
    return !!(active && (active.closest('#ctx-params-container') || active.closest('#ctx-variables-container')));
}

// "prod_start", "prod_start (default)", "never": what actually resets the entry,
// including the file-wide default it inherits when it sets no reset_on.
function describeResetPolicy(resetOn, defaults) {
    const inherited = !Array.isArray(resetOn);
    const events = inherited ? (Array.isArray(defaults) ? defaults : []) : resetOn;
    const text = (events.length === 0 || events.includes('none')) ? 'never' : events.join(', ');
    return inherited ? `${text} (default)` : text;
}

function renderContextEditor(namespace, key, entry, editable) {
    const id = escapeHtml(`ctx-${namespace}-${key}-value`);
    const data = `data-namespace="${escapeHtml(namespace)}" data-key="${escapeHtml(key)}"`;
    const disabled = editable ? '' : 'disabled';
    if (entry.type === 'bool') {
        return `<input id="${id}" ${data} type="checkbox" ${entry.value ? 'checked' : ''} ${disabled}>`;
    }
    if (entry.type === 'int' || entry.type === 'float') {
        const step = entry.type === 'int' ? '1' : 'any';
        const mode = entry.type === 'int' ? 'numeric' : 'decimal';
        return `<input id="${id}" ${data} type="number" step="${step}" inputmode="${mode}" value="${escapeHtml(entry.value)}" ${disabled}>`;
    }
    return `<input id="${id}" ${data} type="text" value="${escapeHtml(entry.value)}" ${disabled}>`;
}

function renderContextCards(namespace, entries, locked, defaultResetEvents) {
    const keys = Object.keys(entries);
    if (keys.length === 0) {
        return `<p class="empty-state">No ${namespace} defined in the context file.</p>`;
    }

    const cards = keys.map((key) => {
        const entry = entries[key];
        const readOnly = !entry.editable_when_idle;
        const editable = !locked && !readOnly;
        const dataAttrs = `data-namespace="${escapeHtml(namespace)}" data-key="${escapeHtml(key)}"`;

        return `
            <div class="ctx-card${readOnly ? ' ctx-card-readonly' : ' ctx-card-editable'}">
                <div class="ctx-card-header">
                    <span class="ctx-card-key">${escapeHtml(key)}</span>
                    <span class="ctx-card-tag">${escapeHtml(entry.type)}</span>
                    ${readOnly ? '<span class="ctx-card-tag ctx-card-tag-readonly" title="editable_when_idle: false — only task logic writes it">read-only</span>' : ''}
                </div>
                <div class="ctx-card-body">
                    <div class="ctx-card-value">${renderContextEditor(namespace, key, entry, editable)}</div>
                    <div class="ctx-card-details">
                        <span><span class="ctx-detail-label">Default:</span> ${escapeHtml(String(entry.default))}</span>
                        <span><span class="ctx-detail-label">Reset:</span> ${escapeHtml(describeResetPolicy(entry.reset_on, defaultResetEvents))}</span>
                        <span><span class="ctx-detail-label">Persist:</span> ${entry.persist ? 'yes' : 'no'}</span>
                        ${entry.description ? `<span class="ctx-card-desc">${escapeHtml(entry.description)}</span>` : ''}
                    </div>
                    <div class="ctx-card-actions">
                        <button type="button" class="btn-task" data-ctx-action="apply" ${dataAttrs} ${editable ? '' : 'disabled'}>Apply</button>
                        <button type="button" class="btn-shutdown" data-ctx-action="reset" ${dataAttrs} ${locked ? 'disabled' : ''}>Reset</button>
                    </div>
                </div>
            </div>
        `;
    }).join('');

    // While a task runs every card is greyed out, not just the read-only ones.
    return `<div class="ctx-cards${locked ? ' ctx-cards-locked' : ''}">${cards}</div>`;
}

function renderContextEmpty(data) {
    const error = data && data.load_error;
    const title = error ? 'Context unavailable' : 'No context configured';
    return `
        <div class="empty-panel">
            <div class="empty-panel-title">${title}</div>
            ${error ? `<p class="empty-panel-text ctx-error">${escapeHtml(error)}</p>` : ''}
            <p class="empty-panel-text">
                Params and variables are optional; the cell runs without them. To use them, add a
                <code>context.yaml</code> next to <code>config.yaml</code> (or set <code>context_file:</code>
                in config.yaml) and restart the app:
            </p>
            <pre class="ctx-snippet">params:
  move_time_s: {type: float, default: 0.5, description: Duration of one move}
variables:
  part_count: {type: int, default: 0, reset_on: prod_start, persist: true}</pre>
            <p class="empty-panel-text">
                Task functions then read and write them through <code>context</code>:
                <code>context.get_param("move_time_s", 0.5)</code>,
                <code>context.set_variable("part_count", n)</code>.
            </p>
        </div>
    `;
}

function renderContext(data) {
    latestContext = data;
    const empty = document.getElementById('ctx-empty');
    const content = document.getElementById('ctx-content');
    if (!empty || !content) return;

    if (!data.enabled) {
        empty.innerHTML = renderContextEmpty(data);
        empty.hidden = false;
        content.hidden = true;
        return;
    }
    empty.hidden = true;
    content.hidden = false;

    const scrollState = captureScrollState('tab-variables');
    const locked = !!data.locked;
    const defaults = data.default_reset_events || [];

    const hint = document.getElementById('ctx-state-hint');
    if (hint) {
        hint.textContent = locked
            ? `Locked while a task runs (${data.state})`
            : 'Editable while no task is running';
        hint.className = 'manual-state-hint ' + (locked ? 'manual-state-blocked' : 'manual-state-ready');
    }

    document.getElementById('ctx-variables-container').innerHTML =
        renderContextCards('variables', data.variables || {}, locked, defaults);
    document.getElementById('ctx-params-container').innerHTML =
        renderContextCards('params', data.params || {}, locked, defaults);

    const resetAll = document.getElementById('ctx-reset-all');
    if (resetAll) resetAll.disabled = locked;

    const source = document.getElementById('ctx-source');
    if (source) {
        source.textContent = `Defined in ${data.source || '—'}`
            + (data.state_file ? ` · persisted values saved to ${data.state_file}` : '');
    }

    ensureContextCardHandlers();
    restoreScrollState('tab-variables', scrollState);
}

// The Apply/Reset buttons carry their target in data-* attributes and are wired
// through one delegated listener per container, rather than an inline
// onclick="...('${key}')" built by string interpolation — a key containing a
// quote would otherwise have broken out of the handler and executed.
// The containers themselves are never replaced (only their innerHTML), so these
// listeners survive every re-render and only need attaching once.
let contextCardHandlersInitialized = false;

function ensureContextCardHandlers() {
    if (contextCardHandlersInitialized) return;

    ['ctx-params-container', 'ctx-variables-container'].forEach((containerId) => {
        const container = document.getElementById(containerId);
        if (!container) return;
        container.addEventListener('click', (event) => {
            const btn = event.target.closest('[data-ctx-action]');
            if (!btn || btn.disabled || !container.contains(btn)) return;

            const namespace = btn.dataset.namespace;
            const key = btn.dataset.key;
            if (!namespace || !key) return;

            if (btn.dataset.ctxAction === 'apply') {
                updateContextValue(namespace, key);
            } else if (btn.dataset.ctxAction === 'reset') {
                resetContext(namespace, key);
            }
        });
        // Enter in a value field applies it, like the Apply button.
        container.addEventListener('keydown', (event) => {
            const input = event.target;
            if (event.key !== 'Enter' || input.tagName !== 'INPUT' || input.disabled) return;
            if (!input.dataset.namespace || !input.dataset.key) return;
            event.preventDefault();
            updateContextValue(input.dataset.namespace, input.dataset.key);
        });
    });

    contextCardHandlersInitialized = true;
}

function refreshContext(auto = false) {
    if (auto && isContextUserInteracting()) {
        return;
    }
    fetch('/api/context')
        .then(r => r.json().then(data => ({ ok: r.ok, data })))
        .then(({ ok, data }) => {
            if (!ok) throw new Error(data.message || 'Failed to load context.');
            renderContext(data);
        })
        .catch(() => {
            if (!latestContext) {
                renderContext({ enabled: false, load_error: 'Failed to load the context from the server.' });
            }
            setContextMessage('Context is temporarily unavailable.', true);
        });
}

function updateContextValue(namespace, key) {
    if (!latestContext || !latestContext[namespace] || !latestContext[namespace][key]) return;

    const entry = latestContext[namespace][key];
    const input = document.getElementById(`ctx-${namespace}-${key}-value`);
    if (!input) return;

    const value = parseFieldValue(entry.type, input);
    if ((entry.type === 'int' || entry.type === 'float') && Number.isNaN(value)) {
        setContextMessage(`Invalid numeric value for ${namespace}.${key}.`, true);
        return;
    }

    const payload = { [namespace]: { [key]: { value } } };
    fetch('/api/context', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    })
        .then(r => r.json())
        .then(d => {
            if (d.success === false) {
                const details = Array.isArray(d.errors) && d.errors.length > 0
                    ? ` (${d.errors.join('; ')})`
                    : '';
                const msg = (d.message || `Failed to update ${namespace}.${key}.`) + details;
                setMessage(msg);
                setContextMessage(msg, true);
            } else {
                const msg = `${namespace}.${key} set to ${value}.`;
                setMessage(msg);
                setContextMessage(msg, false);
            }
            // The input had focus, which pauses the 1 Hz refresh; release it so
            // the card shows the server-side value from here on.
            if (document.activeElement === input) input.blur();
            refreshContext();
        })
        .catch(() => {
            const msg = 'Error updating context value.';
            setMessage(msg);
            setContextMessage(msg, true);
        });
}

function resetContext(namespace, key = null) {
    if (namespace === 'all' || !key) {
        showConfirmModal(
            "Confirm Reset All",
            "Reset all params and variables to their default values? Persisted values are reset too. This cannot be undone.",
            () => executeContextReset(namespace, key)
        );
    } else {
        executeContextReset(namespace, key);
    }
}

function executeContextReset(namespace, key) {
    const payload = { namespace };
    if (key) payload.key = key;

    fetch('/api/context/reset', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    })
        .then(r => r.json())
        .then(d => {
            if (d.success === false) {
                const msg = d.message || 'Failed to reset context.';
                setMessage(msg);
                setContextMessage(msg, true);
            } else {
                const msg = key ? `${namespace}.${key} reset to default.` : 'All params and variables reset to defaults.';
                setMessage(msg);
                setContextMessage(msg, false);
            }
            refreshContext();
        })
        .catch(() => {
            const msg = 'Error resetting context.';
            setMessage(msg);
            setContextMessage(msg, true);
        });
}

function loadRobotInfo() {
    fetch('/api/info')
        .then(r => r.json())
        .then(data => {
            const container = document.getElementById('robot-info-container');
            container.innerHTML = '';
            if (!Array.isArray(data) || data.length === 0) {
                container.innerHTML = '<p class="empty-state">No devices configured.</p>';
                return;
            }
            const statusKeys = new Set(['connected', 'ready', 'faulted', 'device_id']);
            data.forEach(device => {
                const card = document.createElement('div');
                card.className = 'device-card';

                const header = document.createElement('div');
                header.className = 'device-card-header';
                const title = document.createElement('span');
                title.textContent = device.device_id || 'Unknown Device';
                header.appendChild(title);

                const badges = document.createElement('span');
                if ('faulted' in device) {
                    const b = document.createElement('span');
                    b.className = 'badge ' + (device.faulted ? 'badge-error' : 'badge-ok');
                    b.textContent = device.faulted ? 'FAULTED' : 'OK';
                    badges.appendChild(b);
                }
                if ('connected' in device) {
                    const b = document.createElement('span');
                    b.className = 'badge ' + (device.connected ? 'badge-ok' : 'badge-off');
                    b.textContent = device.connected ? 'Connected' : 'Disconnected';
                    badges.appendChild(b);
                }
                if ('ready' in device) {
                    const b = document.createElement('span');
                    b.className = 'badge ' + (device.ready ? 'badge-ok' : 'badge-warn');
                    b.textContent = device.ready ? 'Ready' : 'Not Ready';
                    badges.appendChild(b);
                }
                header.appendChild(badges);
                card.appendChild(header);

                const body = document.createElement('div');
                body.className = 'device-card-body';
                const table = document.createElement('table');
                for (const key in device) {
                    if (statusKeys.has(key)) continue;
                    const row = table.insertRow();
                    const label = row.insertCell(0);
                    const value = row.insertCell(1);
                    label.textContent = key.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
                    const raw = device[key];
                    if (key === 'ip_address' && raw) {
                        const link = document.createElement('a');
                        link.href = `http://${raw}`;
                        link.target = '_blank';
                        link.rel = 'noopener noreferrer';
                        link.textContent = raw;
                        value.appendChild(link);
                    } else {
                        value.textContent = (raw !== null && raw !== undefined) ? raw : '—';
                    }
                }
                body.appendChild(table);
                card.appendChild(body);
                container.appendChild(card);
            });
        })
        .catch(() => {
            document.getElementById('robot-info-container').innerHTML =
            '<p class="empty-state empty-state-error">Error loading device info.</p>';
        });
}

/* ---- Backup / restore tab ---- */
function loadBackupRestoreRobots() {
    fetch('/api/backup_restore/robots')
        .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
        .then(({ ok, data }) => {
            if (!ok) {
                throw new Error(data.message || 'Failed to load Mecademic robots.');
            }
            backupRestoreRobots = Array.isArray(data.robots) ? data.robots : [];
            populateBackupRestoreSelectors();
        })
        .catch((err) => {
            setBackupRestoreMessage(err.message || 'Failed to load Mecademic robots.', true);
        });
}

function populateBackupRestoreSelectors() {
    const backupSelect = document.getElementById('backup-robot-select');
    const restoreSelect = document.getElementById('restore-robot-select');
    if (!backupSelect || !restoreSelect) return;

    const backupCurrent = backupSelect.value;
    const restoreCurrent = restoreSelect.value;

    backupSelect.innerHTML = '<option value="">All Connected Robots (Default)</option>';
    restoreSelect.innerHTML = '<option value="">Select a robot</option>';

    backupRestoreRobots.forEach((robot) => {
        const status = robot.connected ? 'Connected' : 'Disconnected';
        const label = `${robot.device_id} (${status})`;

        const backupOpt = document.createElement('option');
        backupOpt.value = robot.device_id;
        backupOpt.textContent = label;
        backupOpt.disabled = !robot.connected;
        if (backupCurrent === robot.device_id) backupOpt.selected = true;
        backupSelect.appendChild(backupOpt);

        const restoreOpt = document.createElement('option');
        restoreOpt.value = robot.device_id;
        restoreOpt.textContent = label;
        restoreOpt.disabled = !robot.connected;
        if (restoreCurrent === robot.device_id) restoreOpt.selected = true;
        restoreSelect.appendChild(restoreOpt);
    });

    renderBackupRestoreSelectMenu('backup-robot-picker');
    renderBackupRestoreSelectMenu('restore-robot-picker');
}

function setBackupRestoreSelectOpen(picker, isOpen) {
    if (!picker) return;
    const trigger = picker.querySelector('.log-select-trigger');
    if (!trigger) return;
    picker.classList.toggle('open', isOpen);
    trigger.setAttribute('aria-expanded', String(isOpen));
}

function updateBackupRestoreSelectLabel(pickerId) {
    const picker = document.getElementById(pickerId);
    if (!picker) return;

    const sel = picker.querySelector('select');
    const label = picker.querySelector('.log-select-label');
    if (!sel || !label) return;

    const selected = sel.selectedOptions[0];
    if (!selected) {
        label.textContent = 'Select an option';
        return;
    }
    label.textContent = selected.textContent || 'Select an option';
}

function selectBackupRestoreOption(pickerId, value, onChange) {
    const picker = document.getElementById(pickerId);
    if (!picker) return;

    const sel = picker.querySelector('select');
    const menu = picker.querySelector('.log-select-menu');
    if (!sel || !menu) return;

    sel.value = value;
    menu.querySelectorAll('.log-select-option, .log-select-placeholder').forEach((button) => {
        const selected = button.dataset.value === value;
        button.classList.toggle('active', selected);
        button.setAttribute('aria-selected', String(selected));
    });

    updateBackupRestoreSelectLabel(pickerId);
    setBackupRestoreSelectOpen(picker, false);
    if (onChange) onChange();
}

function renderBackupRestoreSelectMenu(pickerId, onChange = null) {
    const picker = document.getElementById(pickerId);
    if (!picker) return;

    const sel = picker.querySelector('select');
    const menu = picker.querySelector('.log-select-menu');
    if (!sel || !menu) return;

    menu.innerHTML = '';

    Array.from(sel.options).forEach((opt) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.dataset.value = opt.value;
        btn.textContent = opt.textContent || '';
        btn.setAttribute('role', 'option');
        btn.setAttribute('aria-selected', String(opt.value === sel.value));

        if (opt.value === '') {
            btn.className = 'log-select-placeholder';
        } else {
            btn.className = 'log-select-option';
        }
        if (opt.value === sel.value) btn.classList.add('active');

        if (opt.disabled) {
            btn.disabled = true;
        } else {
            btn.addEventListener('click', () => selectBackupRestoreOption(pickerId, opt.value, onChange));
        }

        menu.appendChild(btn);
    });

    updateBackupRestoreSelectLabel(pickerId);
}

function ensureBackupRestoreDropdowns() {
    if (backupRestoreDropdownsInitialized) return;

    const ids = ['backup-robot-picker', 'restore-robot-picker'];
    ids.forEach((pickerId) => {
        const picker = document.getElementById(pickerId);
        const trigger = picker?.querySelector('.log-select-trigger');
        if (!picker || !trigger) return;

        trigger.addEventListener('click', () => {
            if (trigger.disabled) return;
            const isOpen = picker.classList.contains('open');
            ids.forEach((otherId) => setBackupRestoreSelectOpen(document.getElementById(otherId), false));
            setBackupRestoreSelectOpen(picker, !isOpen);
        });
    });

    document.addEventListener('click', (event) => {
        const ids = ['backup-robot-picker', 'restore-robot-picker'];
        ids.forEach((pickerId) => {
            const picker = document.getElementById(pickerId);
            if (!picker || picker.contains(event.target)) return;
            setBackupRestoreSelectOpen(picker, false);
        });
    });

    document.addEventListener('keydown', (event) => {
        if (event.key !== 'Escape') return;
        ['backup-robot-picker', 'restore-robot-picker'].forEach((pickerId) => {
            setBackupRestoreSelectOpen(document.getElementById(pickerId), false);
        });
    });

    backupRestoreDropdownsInitialized = true;
}

function runBackupAction() {
    const backupRobotSelect = document.getElementById('backup-robot-select');
    const resultContainer = document.getElementById('backup-result-container');
    if (!backupRobotSelect || !resultContainer) return;

    const robotId = backupRobotSelect.value;
    const mode = robotId ? 'single' : 'all';

    resultContainer.innerHTML = '<p class="empty-state">Running backup...</p>';

    fetch('/api/backup_restore/backup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode, robot_id: robotId || null, stop_on_error: false }),
    })
        .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
        .then(({ ok, data }) => {
            if (!ok) {
                throw new Error(data.message || 'Backup failed.');
            }
            renderBackupResults(data);
            const msg = data.message || 'Backup command finished.';
            setBackupRestoreMessage(msg, !data.success);
        })
        .catch((err) => {
            resultContainer.innerHTML = '<p class="empty-state empty-state-error">Backup failed.</p>';
            setBackupRestoreMessage(err.message || 'Backup request failed.', true);
        });
}

function renderBackupResults(data) {
    const resultContainer = document.getElementById('backup-result-container');
    if (!resultContainer) return;

    const rows = Array.isArray(data.results) ? data.results : [];
    if (rows.length === 0) {
        resultContainer.innerHTML = '<p class="empty-state">No archives were generated.</p>';
        return;
    }

    const cards = rows.map((row) => {
        const outcomeClass = row.success ? 'backup-restore-ok' : 'backup-restore-error';
        const counts = row.counts || {};
        const errorBlock = Array.isArray(row.errors) && row.errors.length
            ? `<ul class="backup-restore-errors">${row.errors.map((err) => `<li>${escapeHtml(err.kind || 'item')}: ${escapeHtml(err.name || 'unknown')} - ${escapeHtml(err.error || 'error')}</li>`).join('')}</ul>`
            : '';
        const download = row.download_url
            ? `<a class="backup-download-link" href="${row.download_url}">Download archive</a>`
            : '<span class="backup-download-disabled">Archive unavailable</span>';
        return `
            <div class="backup-restore-result-card ${outcomeClass}">
                <div class="backup-restore-result-header">
                    <strong>${escapeHtml(row.device_id || 'Unknown')}</strong>
                    <span>${row.success ? 'Success' : 'Completed with errors'}</span>
                </div>
                <div class="backup-restore-result-body">
                    <div>Variables: ${escapeHtml(String(counts.variables ?? 0))}</div>
                    <div>Files: ${escapeHtml(String(counts.files ?? 0))}</div>
                    <div>${download}</div>
                    ${errorBlock}
                </div>
            </div>
        `;
    }).join('');

    resultContainer.innerHTML = `
        <div class="backup-restore-summary">
            Success ${escapeHtml(String(data.success_count ?? 0))} / ${escapeHtml(String(data.target_count ?? rows.length))}
        </div>
        <div class="backup-restore-result-grid">${cards}</div>
    `;
}

function openRestoreArchivePicker() {
    const input = document.getElementById('restore-archive-input');
    if (input) input.click();
}

function setSelectedRestoreArchive(file) {
    const chip = document.getElementById('restore-file-chip');
    const dropzone = document.getElementById('restore-dropzone');
    if (!chip || !dropzone) return;

    if (!file) {
        restoreArchiveFile = null;
        chip.textContent = 'No archive selected.';
        dropzone.classList.remove('restore-dropzone-ready');
        return;
    }

    if (!String(file.name || '').toLowerCase().endsWith('.zip')) {
        restoreArchiveFile = null;
        chip.textContent = 'Invalid file type. Please choose a .zip archive.';
        dropzone.classList.remove('restore-dropzone-ready');
        setBackupRestoreMessage('Invalid restore file. Only .zip is supported.', true);
        return;
    }

    restoreArchiveFile = file;
    chip.textContent = `${file.name} (${Math.round(file.size / 1024)} KB)`;
    dropzone.classList.add('restore-dropzone-ready');
}

function initRestoreDropzone() {
    const dropzone = document.getElementById('restore-dropzone');
    const fileInput = document.getElementById('restore-archive-input');
    if (!dropzone || !fileInput) return;

    fileInput.addEventListener('change', () => {
        const file = fileInput.files && fileInput.files.length > 0 ? fileInput.files[0] : null;
        setSelectedRestoreArchive(file);
    });

    ['dragenter', 'dragover'].forEach((eventName) => {
        dropzone.addEventListener(eventName, (event) => {
            event.preventDefault();
            event.stopPropagation();
            dropzone.classList.add('restore-dropzone-hover');
        });
    });

    ['dragleave', 'drop'].forEach((eventName) => {
        dropzone.addEventListener(eventName, (event) => {
            event.preventDefault();
            event.stopPropagation();
            dropzone.classList.remove('restore-dropzone-hover');
        });
    });

    dropzone.addEventListener('drop', (event) => {
        const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files.length > 0
            ? event.dataTransfer.files[0]
            : null;
        setSelectedRestoreArchive(file);
    });
}

function runRestoreAction(dryRun) {
    const robotSelect = document.getElementById('restore-robot-select');
    const resultContainer = document.getElementById('restore-result-container');
    if (!robotSelect || !resultContainer) return;

    const robotId = robotSelect.value;
    if (!robotId) {
        setBackupRestoreMessage('Select a connected robot for restore.', true);
        return;
    }
    if (!restoreArchiveFile) {
        setBackupRestoreMessage('Select a .zip archive to restore.', true);
        return;
    }

    const formData = new FormData();
    formData.append('archive', restoreArchiveFile);
    formData.append('robot_id', robotId);
    formData.append('dry_run', dryRun ? 'true' : 'false');
    formData.append('stop_on_error', 'false');

    resultContainer.innerHTML = `<p class="empty-state">${dryRun ? 'Running restore dry-run...' : 'Running restore...'}</p>`;

    fetch('/api/backup_restore/restore', {
        method: 'POST',
        body: formData,
    })
        .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
        .then(({ ok, data }) => {
            if (!ok) {
                throw new Error(data.message || 'Restore failed.');
            }
            renderRestoreResult(data);
            setBackupRestoreMessage(data.message || 'Restore finished.', !data.success);
        })
        .catch((err) => {
            resultContainer.innerHTML = '<p class="empty-state empty-state-error">Restore failed.</p>';
            setBackupRestoreMessage(err.message || 'Restore request failed.', true);
        });
}

function renderRestoreResult(data) {
    const container = document.getElementById('restore-result-container');
    if (!container) return;

    const counts = data.counts || {};
    const errors = Array.isArray(data.errors) ? data.errors : [];
    const errorBlock = errors.length
        ? `<ul class="backup-restore-errors">${errors.map((err) => `<li>${escapeHtml(err.kind || 'item')}: ${escapeHtml(err.name || 'unknown')} - ${escapeHtml(err.error || 'error')}</li>`).join('')}</ul>`
        : '<p class="empty-state">No item errors reported.</p>';

    container.innerHTML = `
        <div class="backup-restore-result-card ${data.success ? 'backup-restore-ok' : 'backup-restore-error'}">
            <div class="backup-restore-result-header">
                <strong>${escapeHtml(data.device_id || 'Unknown')}</strong>
                <span>${data.dry_run ? 'Dry-run' : (data.success ? 'Success' : 'Completed with errors')}</span>
            </div>
            <div class="backup-restore-result-body">
                <div>Variables: ${escapeHtml(String(counts.variables ?? 0))}</div>
                <div>Files: ${escapeHtml(String(counts.files ?? 0))}</div>
                ${errorBlock}
            </div>
        </div>
    `;
}

/* ---- Log viewer ---- */
function setLogDropdownOpen(isOpen) {
    const picker = document.getElementById('log-file-picker');
    const trigger = document.getElementById('log-file-trigger');
    if (!picker || !trigger) return;
    picker.classList.toggle('open', isOpen);
    trigger.setAttribute('aria-expanded', String(isOpen));
}

function updateLogSelectionLabel() {
    const sel = document.getElementById('log-file-select');
    const picker = document.getElementById('log-file-picker');
    const label = picker ? picker.querySelector('.log-select-label') : null;
    if (!sel || !label) return;

    const selected = sel.selectedOptions[0];
    if (!selected || !selected.value) {
        label.textContent = 'Select a log file';
        return;
    }

    const group = selected.parentElement?.label;
    label.textContent = group ? `${group} / ${selected.textContent}` : selected.textContent;
}

function selectLogFile(value, shouldLoad = true) {
    const sel = document.getElementById('log-file-select');
    const menu = document.getElementById('log-file-menu');
    if (!sel || !menu) return;

    sel.value = value;
    menu.querySelectorAll('.log-select-option').forEach((button) => {
        button.classList.toggle('active', button.dataset.value === value);
        button.setAttribute('aria-selected', String(button.dataset.value === value));
    });
    updateLogSelectionLabel();
    setLogDropdownOpen(false);
    if (shouldLoad) loadSelectedLog();
}

function renderLogFileMenu(data, currentValue) {
    const menu = document.getElementById('log-file-menu');
    if (!menu) return;

    menu.innerHTML = '';

    const placeholder = document.createElement('button');
    placeholder.type = 'button';
    placeholder.className = 'log-select-placeholder';
    placeholder.textContent = 'Select a log file';
    placeholder.setAttribute('role', 'option');
    placeholder.setAttribute('aria-selected', String(currentValue === ''));
    placeholder.addEventListener('click', () => selectLogFile(''));
    menu.appendChild(placeholder);

    for (const [category, files] of Object.entries(data)) {
        if (!files.length) continue;

        const group = document.createElement('div');
        group.className = 'log-select-group';

        const heading = document.createElement('div');
        heading.className = 'log-select-group-label';
        heading.textContent = category;
        group.appendChild(heading);

        files.forEach((fileName) => {
            const value = `${category}/${fileName}`;
            const option = document.createElement('button');
            option.type = 'button';
            option.className = 'log-select-option';
            option.dataset.value = value;
            option.textContent = fileName;
            option.setAttribute('role', 'option');
            option.setAttribute('aria-selected', String(value === currentValue));
            if (value === currentValue) option.classList.add('active');
            option.addEventListener('click', () => selectLogFile(value));
            group.appendChild(option);
        });

        menu.appendChild(group);
    }
}

function ensureLogDropdown() {
    if (logDropdownInitialized) return;

    const picker = document.getElementById('log-file-picker');
    const trigger = document.getElementById('log-file-trigger');
    if (!picker || !trigger) return;

    trigger.addEventListener('click', () => {
        setLogDropdownOpen(!picker.classList.contains('open'));
    });

    document.addEventListener('click', (event) => {
        if (!picker.contains(event.target)) setLogDropdownOpen(false);
    });

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') setLogDropdownOpen(false);
    });

    logDropdownInitialized = true;
}

function populateLogFileList() {
    ensureLogDropdown();

    fetch('/api/logs')
        .then(r => r.json())
        .then(data => {
            const sel = document.getElementById('log-file-select');
            const current = sel.value;
            sel.innerHTML = '<option value="">Select a log file</option>';
            for (const [category, files] of Object.entries(data)) {
                if (files.length === 0) continue;
                const group = document.createElement('optgroup');
                group.label = category;
                files.forEach(f => {
                    const opt = document.createElement('option');
                    opt.value = `${category}/${f}`;
                    opt.textContent = f;
                    if (opt.value === current) opt.selected = true;
                    group.appendChild(opt);
                });
                sel.appendChild(group);
            }

            if (current && !sel.querySelector(`option[value="${CSS.escape(current)}"]`)) {
                sel.value = '';
            }

            renderLogFileMenu(data, sel.value);
            updateLogSelectionLabel();
        })
        .catch(() => {});
}

function coloriseLine(line) {
    const escaped = line.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    if (/\|\s*CRITICAL\s*\|/i.test(line)) return `<span class="log-critical">${escaped}</span>`;
    if (/\|\s*ERROR\s*\|/i.test(line))    return `<span class="log-error">${escaped}</span>`;
    if (/\|\s*WARNING\s*\|/i.test(line))  return `<span class="log-warning">${escaped}</span>`;
    if (/\|\s*INFO\s*\|/i.test(line))     return `<span class="log-info">${escaped}</span>`;
    if (/\|\s*DEBUG\s*\|/i.test(line))    return `<span class="log-debug">${escaped}</span>`;
    return escaped;
}

// Severity ranking used by the "Min level" filter. Lines whose level sits below
// the selected threshold are hidden client-side, so switching level is instant
// and needs no refetch.
const LOG_LEVEL_RANK = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };
const LOG_LEVEL_RE = /\|\s*(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s*\|/;

// Raw lines from the last fetch, kept so filtering does not hit the server.
let currentLogLines = [];

function lineLevelRank(line) {
    const m = LOG_LEVEL_RE.exec(line);
    // Continuation lines (tracebacks) have no level of their own; keep them
    // visible so a stack trace is never silently truncated by the filter.
    return m ? LOG_LEVEL_RANK[m[1]] : Number.MAX_SAFE_INTEGER;
}

function renderFilteredLog() {
    const output = document.getElementById('log-output');
    const countEl = document.getElementById('log-match-count');
    if (!output) return;

    const levelSel = document.getElementById('log-level-select');
    const searchEl = document.getElementById('log-search');
    const minRank = LOG_LEVEL_RANK[levelSel ? levelSel.value : 'ALL'] || 0;
    const needle = (searchEl ? searchEl.value : '').trim().toLowerCase();

    const filtered = currentLogLines.filter((line) => {
        if (lineLevelRank(line) < minRank) return false;
        if (needle && !line.toLowerCase().includes(needle)) return false;
        return true;
    });

    if (countEl) {
        countEl.textContent = currentLogLines.length
            ? `${filtered.length} / ${currentLogLines.length} lines`
            : '';
    }

    if (currentLogLines.length === 0) {
        output.innerText = 'Select a log file above.';
        return;
    }
    if (filtered.length === 0) {
        output.innerText = 'No lines match the current level/text filter.';
        return;
    }

    output.innerHTML = filtered.map(coloriseLine).join('');
    const autoscroll = document.getElementById('log-autoscroll');
    if (autoscroll && autoscroll.checked) {
        output.scrollTop = output.scrollHeight;
    }
}

function loadSelectedLog() {
    const sel = document.getElementById('log-file-select');
    const val = sel.value;
    const output = document.getElementById('log-output');
    if (!val) {
        currentLogLines = [];
        output.innerText = 'Select a log file above.';
        renderFilteredLog();
        return;
    }
    fetch(`/api/logs/${val}?lines=500`)
        .then(r => r.json())
        .then(data => {
            if (data.message) { output.innerText = data.message; return; }
            currentLogLines = Array.isArray(data.lines) ? data.lines : [];
            renderFilteredLog();
        })
        .catch(() => { output.innerText = 'Error loading log file.'; });
}

// Auto-refresh log if the Logs tab is visible
setInterval(() => {
    if (document.getElementById('tab-logs').classList.contains('active')) {
        loadSelectedLog();
    }
}, 3000);

setInterval(() => {
    const tab = document.getElementById('tab-backup-restore');
    if (tab && tab.classList.contains('active')) {
        loadBackupRestoreRobots();
    }
}, 5000);

setInterval(() => {
    if (document.getElementById('tab-production').classList.contains('active')) {
        refreshProdMetrics();
    }
    if (document.getElementById('tab-variables').classList.contains('active')) {
        refreshContext(true);
    }
}, 1000);

/* ---- Custom view link ---- */
// Shown only when the workspace ships custom_view/index.html.
function initCustomViewLink() {
    fetch('/api/data')
        .then(r => r.json())
        .then(info => {
            document.getElementById('custom-view-link').hidden = !(info && info.view_available);
        })
        .catch(() => {});
}

/* ---- Startup ---- */
setInterval(updateRobotStatus, 100);
setInterval(loadRobotInfo, 1000);
updateRobotStatus();
loadRobotInfo();
refreshProdMetrics();
refreshContext();
loadManualActions();
initRestoreDropzone();
ensureBackupRestoreDropdowns();
renderBackupRestoreSelectMenu('backup-robot-picker');
renderBackupRestoreSelectMenu('restore-robot-picker');
loadBackupRestoreRobots();
initCustomViewLink();
