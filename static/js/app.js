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
    if (name === 'devices') {
        loadRobotInfo();
    }
}

/* ---- Status polling ---- */
// From the SSE `status` event, or the /api/status poll when the stream is down.
function applyStatus(data) {
    latestControllerStatus = data.status;
    patchLiveStep(data.step || null);
    stateDisplays().forEach((el) => renderStatusBox(el, data.status, data.task || null));
    updateManualActionAvailability(data.status);
    updateWorkspaceButtons(data.status);
    // Another page switched the workspace: everything here is stale.
    if (data.workspace) {
        if (currentWorkspace === null) currentWorkspace = data.workspace;
        else if (data.workspace !== currentWorkspace && !workspaceSwitching) window.location.reload();
    }
}

function updateRobotStatus() {
    fetch('/api/status')
        .then(r => r.json())
        .then(applyStatus)
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
        .catch(() => setMessage('Error sending initialization command.'))
        .finally(loadSystemInfo);
}

/* ---- Workspace picker ---- */
let currentWorkspace = null;
let workspaceSwitching = false;

function loadWorkspaces() {
    fetch('/api/workspaces')
        .then(r => r.json())
        .then(renderWorkspaces)
        .catch(() => {
            const hint = document.getElementById('workspace-hint');
            if (hint) hint.textContent = 'Could not list workspaces.';
        });
}

function renderWorkspaces(data) {
    const select = document.getElementById('workspace-select');
    const nameEl = document.getElementById('workspace-name');
    const hint = document.getElementById('workspace-hint');
    const items = Array.isArray(data.workspaces) ? data.workspaces : [];
    const current = items.find((w) => w.current);
    if (nameEl) nameEl.textContent = current ? (current.title || current.name) : (data.current || '');
    if (select) {
        select.innerHTML = items.map((w) => {
            const label = w.title ? `${w.title} (${w.name})` : w.name;
            const suffix = w.valid ? '' : ' — invalid config.yaml';
            return `<option value="${escapeHtml(w.name)}"${w.current ? ' selected' : ''}${w.valid ? '' : ' disabled'}>`
                + `${escapeHtml(label + suffix)}</option>`;
        }).join('');
    }
    if (hint) {
        hint.textContent = data.root
            ? `Folders of ${data.root} holding a config.yaml.`
            : 'Set MECADEMIC_WORKSPACES_ROOT (or --workspaces-root) to list other workspaces.';
    }
    updateWorkspaceButtons(latestControllerStatus);
}

function updateWorkspaceButtons(status) {
    const normalized = normalizeStatus(status);
    const locked = workspaceSwitching || normalized === 'BUSY' || normalized === 'INITIALIZING';
    ['workspace-load-btn', 'workspace-reload-btn', 'workspace-select'].forEach((id) => {
        const el = document.getElementById(id);
        if (el) {
            el.disabled = locked;
            el.title = locked && !workspaceSwitching ? `Not available while the controller is ${status}.` : '';
        }
    });
}

function switchWorkspaceTo(name, title, message) {
    showConfirmModal(title, message, () => {
        workspaceSwitching = true;
        updateWorkspaceButtons(latestControllerStatus);
        setMessage(`Loading workspace '${name}'…`);
        fetch('/api/workspaces/switch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name }),
        })
            .then(r => r.json().then((data) => ({ ok: r.ok, data })))
            .then(({ ok, data }) => {
                setMessage(data.message);
                // Reload whatever the outcome once a switch happened: tabs,
                // manual actions and the custom view all belong to the workspace.
                if (ok) window.location.reload();
            })
            .catch(() => setMessage('Error switching workspace.'))
            .finally(() => {
                workspaceSwitching = false;
                updateWorkspaceButtons(latestControllerStatus);
            });
    });
}

function loadSelectedWorkspace() {
    const select = document.getElementById('workspace-select');
    const name = select ? select.value : '';
    if (!name) return;
    if (name === currentWorkspace) {
        reloadCurrentWorkspace();
        return;
    }
    switchWorkspaceTo(name, 'Load workspace',
        `Load workspace '${name}'?\n\nEvery device is disconnected and this workspace's code is unloaded. `
        + 'The new workspace starts OFF: press INITIALIZE to connect its devices.');
}

function reloadCurrentWorkspace() {
    if (!currentWorkspace) return;
    switchWorkspaceTo(currentWorkspace, 'Reload workspace code',
        `Reload the code and config.yaml of '${currentWorkspace}'?\n\n`
        + 'Every device is disconnected; press INITIALIZE afterwards.');
}

/* ---- Workspace / config problems ---- */
function loadSystemInfo() {
    fetch('/api/system')
        .then(r => r.json())
        .then(renderConfigBanner)
        .catch(() => {});
}

function renderConfigBanner(info) {
    const banner = document.getElementById('config-banner');
    if (!banner) return;
    const issues = Array.isArray(info?.config_issues) ? info.config_issues : [];
    const lines = [];
    if (info?.controller_error) {
        lines.push({ level: 'error', text: `Controller failed to start: ${info.controller_error}` });
    }
    issues.forEach((item) => {
        lines.push({ level: item.level, text: `${item.device ? item.device + ': ' : ''}${item.message}` });
    });
    if (lines.length === 0) {
        banner.hidden = true;
        banner.innerHTML = '';
        return;
    }
    const hasError = lines.some((l) => l.level === 'error');
    banner.className = 'config-banner ' + (hasError ? 'config-banner-error' : 'config-banner-warning');
    banner.innerHTML = `<strong>config.yaml ${hasError ? 'errors' : 'warnings'}</strong>`
        + (hasError ? ' &mdash; INITIALIZE is blocked until they are fixed.' : '')
        + '<ul>' + lines.map((l) => `<li class="config-issue-${escapeHtml(l.level)}">${escapeHtml(l.text)}</li>`).join('') + '</ul>';
    banner.hidden = false;
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

    // Clearing faults aborts a running task server-side: never offer it mid-task.
    const clearBtn = document.getElementById('manual-clear-faults-btn');
    if (clearBtn) {
        const locked = normalizedStatus === 'BUSY' || normalizedStatus === 'INITIALIZING';
        clearBtn.disabled = locked;
        clearBtn.title = locked ? `Not available while the controller is ${status}.` : '';
    }
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

    showConfirmModal(confirmTitle, confirmMessage + detachedDevicesWarning(), () => {
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

// Same request as the Control tab's CLEAR FAULTS, reported in the Manual log
// with the server's detail (which devices are still faulted, which errored).
function sendManualClearFaults() {
    showConfirmModal(
        'Confirm Clear Faults',
        'Clear the faults on every connected device? No hardware moves; released devices are left alone.',
        () => {
            fetch('/api/clear_faults', { method: 'POST' })
                .then(r => r.json())
                .then((d) => {
                    setMessage(d.message);
                    setManualActionMessage(d.message, d.success === false);
                })
                .catch(() => {
                    setMessage('Error sending clear faults command.');
                    setManualActionMessage('Error sending clear faults command.', true);
                });
        }
    );
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
    refreshProdTimelines(data.steps ? data.steps.timeline_rev : undefined);
    // A run was archived (or this is the first load): the disk history changed.
    // Keyed on the last run's end, since the in-memory list is capped.
    const runs = (data.metrics || {}).run_history || [];
    const runKey = runs.length ? `${runs.length}|${runs[runs.length - 1].run_end}` : '0';
    if (runKey !== prodHistoryRunKey) {
        prodHistoryRunKey = runKey;
        refreshProdHistory();
    }
}

/* ---- Cycle timeline (Gantt) ----
 * One row per top-level step, in declared order (steps a cycle skipped keep
 * an empty row, so a conditional branch shows as such); nested steps are thin
 * bars inside their parent's row, and a dashed box marks each step's average.
 * Hand-drawn SVG: no chart library on a show floor. Fetched only when a cycle
 * finished (timeline_rev changed).
 */
let prodTimelines = { rev: -1, order: [], timelines: [], avg_s: {} };
let prodTimelineSelected = null;   // "run_id:cycle", or null to follow the latest
let prodTimelineFetching = false;
const TIMELINE_SLOTS = 30;

function refreshProdTimelines(rev) {
    if (rev === undefined || rev === prodTimelines.rev || prodTimelineFetching) return;
    prodTimelineFetching = true;
    fetch(`/api/prod/timelines?after_rev=${prodTimelines.rev}`)
        .then(r => r.json())
        .then((data) => {
            if (data.unchanged) return;
            prodTimelines = data;
            renderProdTimelines();
        })
        .catch(() => {})
        .finally(() => { prodTimelineFetching = false; });
}

function timelineKey(t) {
    return `${t.run_id}:${t.cycle}`;
}

function selectTimeline(key) {
    prodTimelineSelected = key;
    renderProdTimelines();
}

function niceTimelineStep(span, target = 6) {
    const steps = [0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
    return steps.find((s) => span / s <= target) || steps[steps.length - 1];
}

function renderProdTimelines() {
    const strip = document.getElementById('prod-timeline-strip');
    const chart = document.getElementById('prod-timeline-chart');
    const caption = document.getElementById('prod-timeline-caption');
    if (!strip || !chart || !caption) return;

    const timelines = Array.isArray(prodTimelines.timelines) ? prodTimelines.timelines : [];
    if (timelines.length === 0) {
        strip.innerHTML = '';
        chart.innerHTML = '';
        caption.textContent = 'No finished cycle yet. Start production to fill the timeline.';
        return;
    }
    let selected = timelines.find((t) => timelineKey(t) === prodTimelineSelected);
    if (!selected) {
        prodTimelineSelected = null;
        selected = timelines[timelines.length - 1];
    }

    // --- Strip: one bar per finished cycle, height = duration ---
    const stripW = Math.max(strip.clientWidth || 600, 200);
    const stripH = 44;
    const slotW = stripW / Math.max(TIMELINE_SLOTS, timelines.length);
    const maxDuration = Math.max(...timelines.map((t) => t.duration_s || 0), 0.001);
    const bars = timelines.map((t, i) => {
        const h = Math.max(3, (t.duration_s / maxDuration) * (stripH - 4));
        const key = timelineKey(t);
        const cls = `timeline-strip-bar gantt-${escapeHtml(t.status)}${t === selected ? ' selected' : ''}`;
        return `<rect class="${cls}" data-key="${escapeHtml(key)}" x="${(i * slotW + 1).toFixed(1)}" y="${(stripH - h).toFixed(1)}"`
            + ` width="${Math.max(2, slotW - 2).toFixed(1)}" height="${h.toFixed(1)}">`
            + `<title>Cycle ${escapeHtml(t.cycle)} · ${formatDurationSeconds(t.duration_s)} · ${escapeHtml(t.status)}</title></rect>`;
    }).join('');
    strip.innerHTML = `<svg width="${stripW}" height="${stripH}" role="img" aria-label="Duration of the last ${timelines.length} cycles">${bars}</svg>`;

    // --- Chart: the selected cycle ---
    const frames = Array.isArray(selected.frames) ? selected.frames : [];
    const order = (Array.isArray(prodTimelines.order) ? prodTimelines.order : []).slice();
    frames.forEach((f) => { if (f.depth === 0 && !order.includes(f.name)) order.push(f.name); });
    const rowOf = new Map(order.map((name, i) => [name, i]));

    const width = Math.max(chart.clientWidth || 600, 320);
    const labelW = Math.min(190, Math.round(width * 0.34));
    const plotW = width - labelW - 14;
    const rowH = 26;
    const top = 22;
    const height = top + order.length * rowH + 4;
    const span = Math.max(selected.duration_s || 0, 0.001);
    const x = (seconds) => labelW + (Math.max(0, seconds) / span) * plotW;

    const parts = [];
    const tickStep = niceTimelineStep(span);
    for (let t = 0; t <= span + 1e-9; t += tickStep) {
        const tx = x(t).toFixed(1);
        parts.push(`<line class="gantt-grid" x1="${tx}" y1="${top - 4}" x2="${tx}" y2="${height}"></line>`);
        parts.push(`<text class="gantt-tick" x="${tx}" y="${top - 8}" text-anchor="middle">${+t.toFixed(2)} s</text>`);
    }
    order.forEach((name, i) => {
        const y = top + i * rowH;
        if (i % 2 === 1) parts.push(`<rect class="gantt-row-alt" x="0" y="${y}" width="${width}" height="${rowH}"></rect>`);
        const label = name.length > 26 ? name.slice(0, 25) + '…' : name;
        parts.push(`<text class="gantt-label" x="${labelW - 8}" y="${y + rowH / 2 + 4}" text-anchor="end">`
            + `${escapeHtml(label)}<title>${escapeHtml(name)}</title></text>`);
    });

    let busy = 0;
    frames.forEach((f) => {
        const rootName = f.depth === 0 ? f.name : String(f.path || '').split(' / ')[0];
        const row = rowOf.get(rootName);
        if (row === undefined) return;
        const y = top + row * rowH;
        const bx = x(f.offset_s);
        const bw = Math.max(1.5, x(f.offset_s + f.duration_s) - bx);
        const tip = `<title>${escapeHtml(f.path || f.name)}: ${formatDurationSeconds(f.duration_s)} (${escapeHtml(f.status)}), from +${(+f.offset_s).toFixed(2)} s</title>`;
        if (f.depth === 0) {
            busy += f.duration_s;
            parts.push(`<rect class="gantt-bar gantt-${escapeHtml(f.status)}" x="${bx.toFixed(1)}" y="${y + 5}" width="${bw.toFixed(1)}" height="${rowH - 12}">${tip}</rect>`);
            const avg = prodTimelines.avg_s ? prodTimelines.avg_s[f.name] : null;
            if (avg) {
                const aw = Math.max(1.5, x(f.offset_s + avg) - bx);
                parts.push(`<rect class="gantt-avg" x="${bx.toFixed(1)}" y="${y + 3}" width="${aw.toFixed(1)}" height="${rowH - 8}"><title>${escapeHtml(f.name)} average: ${formatDurationSeconds(avg)}</title></rect>`);
            }
        } else {
            parts.push(`<rect class="gantt-sub gantt-${escapeHtml(f.status)}" x="${bx.toFixed(1)}" y="${y + rowH - 8}" width="${bw.toFixed(1)}" height="4">${tip}</rect>`);
        }
    });
    chart.innerHTML = `<svg width="${width}" height="${height}" role="img" aria-label="Steps of cycle ${escapeHtml(selected.cycle)}">${parts.join('')}</svg>`;

    const idle = Math.max(0, (selected.duration_s || 0) - busy);
    const follow = prodTimelineSelected
        ? ' <button type="button" class="link-btn" onclick="selectTimeline(null)">Follow latest &rarr;</button>'
        : '';
    caption.innerHTML = `<strong>Cycle ${escapeHtml(selected.cycle)}</strong> · ${escapeHtml(formatDurationSeconds(selected.duration_s))}`
        + ` · <span class="timeline-status gantt-text-${escapeHtml(selected.status)}">${escapeHtml(selected.status)}</span>`
        + ` · steps ${escapeHtml(formatDurationSeconds(busy))}, idle ${escapeHtml(formatDurationSeconds(idle))}`
        + ` · ${escapeHtml(formatTimestamp(selected.started_at))}${follow}`;
}

/* ---- Production history (on disk) ---- */
let prodHistoryRunKey = null;

function refreshProdHistory() {
    Promise.all([
        fetch('/api/history/days').then(r => r.json()),
        fetch('/api/history/runs?limit=10').then(r => r.json()),
    ])
        .then(([days, runs]) => renderProdHistory(days, runs))
        .catch(() => {
            const container = document.getElementById('prod-history-container');
            if (container) container.innerHTML = '<p class="empty-state empty-state-error">Could not load the history.</p>';
        });
}

function renderProdHistory(daysData, runsData) {
    const container = document.getElementById('prod-history-container');
    if (!container) return;
    if (daysData.enabled === false) {
        container.innerHTML = '<p class="empty-state">Production history is disabled (<code>history: {enabled: false}</code> in config.yaml).</p>';
        return;
    }
    const days = Array.isArray(daysData.days) ? daysData.days : [];
    const runs = Array.isArray(runsData.runs) ? runsData.runs.slice().reverse() : [];
    const dash = (v) => (v === null || v === undefined ? '—' : v);

    const dayRows = days.map((d) => `
        <tr>
            <td>${escapeHtml(d.date)}</td>
            <td>${escapeHtml(d.cycles)}</td>
            <td>${escapeHtml(d.ok)}</td>
            <td>${escapeHtml(d.failed)}</td>
            <td>${escapeHtml(d.interrupted)}</td>
            <td>${escapeHtml(formatDurationSeconds(d.average_ok_cycle_s))}</td>
            <td>${escapeHtml(formatDurationSeconds(d.cycle_time_s))}</td>
            <td><a class="link-btn" href="/api/history/cycles.csv?date=${encodeURIComponent(d.date)}" download>CSV &darr;</a></td>
        </tr>`).join('');
    const runRows = runs.map((r) => `
        <tr>
            <td>${escapeHtml(formatTimestamp(r.run_start))}</td>
            <td>${escapeHtml(dash(r.end_event))}</td>
            <td>${escapeHtml(dash(r.cycle_count))}</td>
            <td>${escapeHtml(dash(r.failed_cycles))}</td>
            <td>${escapeHtml(formatDurationSeconds(r.average_cycle_duration_s))}</td>
            <td>${escapeHtml(formatDurationSeconds(r.elapsed_production_s))}</td>
        </tr>`).join('');

    container.innerHTML = `
        <div class="history-block">
            <div class="prod-metric-history-title">Per day</div>
            ${days.length ? `<div class="history-table-wrap"><table class="history-table">
                <thead><tr><th>Date</th><th>Cycles</th><th>OK</th><th>Failed</th><th>Interrupted</th><th>Avg OK cycle</th><th>Cycle time</th><th></th></tr></thead>
                <tbody>${dayRows}</tbody></table></div>`
                : '<p class="prod-metric-empty">No cycle recorded yet.</p>'}
        </div>
        <div class="history-block">
            <div class="prod-metric-history-title">Last runs</div>
            ${runs.length ? `<div class="history-table-wrap"><table class="history-table">
                <thead><tr><th>Started</th><th>Ended by</th><th>Cycles</th><th>Failed</th><th>Avg cycle</th><th>Duration</th></tr></thead>
                <tbody>${runRows}</tbody></table></div>`
                : '<p class="prod-metric-empty">No archived run yet.</p>'}
        </div>`;
}

function initProdTimeline() {
    const strip = document.getElementById('prod-timeline-strip');
    if (strip) {
        strip.addEventListener('click', (event) => {
            const bar = event.target.closest('[data-key]');
            if (bar) selectTimeline(bar.dataset.key);
        });
    }
    let resizeTimer = null;
    window.addEventListener('resize', () => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(renderProdTimelines, 150);
    });
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

/* ---- Devices: health strip (Control tab) and cards (Devices tab) ---- */
let latestDevices = [];
const devicePending = new Set();   // device ids with a link request in flight

// Live/status fields the cards show as badges or buttons, not as table rows.
const DEVICE_STATUS_KEYS = new Set([
    'connected', 'ready', 'faulted', 'device_id', 'link', 'detached',
    'in_transition', 'supports_reconnect', 'supports_monitoring', 'has_live_data',
]);

function setDevicesMessage(text, isError = false) {
    const log = document.getElementById('devices-action-log');
    if (!log) return;
    log.textContent = text || 'No recent device action.';
    log.className = 'message-log ' + (isError ? 'prod-locked' : 'prod-unlocked');
}

function loadRobotInfo() {
    fetch('/api/info')
        .then(r => r.json())
        .then(data => renderDevices(Array.isArray(data) ? data : []))
        .catch(() => {
            const msg = '<p class="empty-state empty-state-error">Error loading device info.</p>';
            document.getElementById('robot-info-container').innerHTML = msg;
            document.getElementById('device-health-strip').innerHTML = msg;
        });
}

function renderDevices(devices) {
    latestDevices = devices;
    renderDeviceHealthStrip(devices);
    renderDeviceCards(devices);
}

// One summary per device: [dot class, short text].
function deviceHealth(device) {
    if (device.in_transition) return ['info', 'Switching…'];
    if (device.detached) {
        return device.link === 'monitoring' ? ['info', 'Released · monitoring'] : ['info', 'Released'];
    }
    if (device.faulted) return ['error', 'Faulted'];
    if (!device.connected) return ['', 'Disconnected'];
    if (!device.ready) return ['warn', 'Not ready'];
    return ['ok', 'Ready'];
}

// Rebuilt every second: it holds no controls, so nothing loses focus.
function renderDeviceHealthStrip(devices) {
    const strip = document.getElementById('device-health-strip');
    if (!strip) return;
    if (devices.length === 0) {
        strip.innerHTML = '<p class="empty-state">No devices configured.</p>';
        return;
    }
    strip.innerHTML = devices.map((device) => {
        const [dot, text] = deviceHealth(device);
        return `<span class="device-chip" title="${escapeHtml(text)}">`
            + `<span class="device-chip-dot ${dot}"></span>`
            + `<span>${escapeHtml(device.device_id || 'device')}</span>`
            + `<span class="device-chip-link">${escapeHtml(text)}</span></span>`;
    }).join('');
}

function deviceBadgesHtml(device) {
    const badges = [];
    if (device.detached) {
        badges.push('<span class="badge badge-info">RELEASED</span>');
    } else if ('faulted' in device) {
        badges.push(device.faulted
            ? '<span class="badge badge-error">FAULTED</span>'
            : '<span class="badge badge-ok">OK</span>');
    }
    const link = device.link || (device.connected ? 'control' : 'disconnected');
    if (link === 'control') badges.push('<span class="badge badge-ok">Connected</span>');
    else if (link === 'monitoring') badges.push('<span class="badge badge-info">Monitoring</span>');
    else badges.push('<span class="badge badge-off">Disconnected</span>');
    if (!device.detached && 'ready' in device) {
        badges.push(device.ready
            ? '<span class="badge badge-ok">Ready</span>'
            : '<span class="badge badge-warn">Not Ready</span>');
    }
    return badges.join('');
}

function deviceTableHtml(device) {
    const rows = [];
    for (const key in device) {
        if (DEVICE_STATUS_KEYS.has(key)) continue;
        const label = key.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
        const raw = device[key];
        let value;
        if (key === 'ip_address' && raw) {
            value = `<a href="http://${escapeHtml(raw)}" target="_blank" rel="noopener noreferrer">${escapeHtml(raw)}</a>`;
        } else {
            value = (raw !== null && raw !== undefined) ? escapeHtml(raw) : '—';
        }
        rows.push(`<tr><td>${escapeHtml(label)}</td><td>${value}</td></tr>`);
    }
    return `<table>${rows.join('')}</table>`;
}

function createDeviceCard(deviceId) {
    const card = document.createElement('div');
    card.className = 'device-card';
    card.dataset.deviceId = deviceId;
    card.innerHTML = `
        <div class="device-card-header">
            <span class="device-card-title"></span>
            <span class="device-card-badges"></span>
        </div>
        <div class="device-card-actions">
            <button type="button" class="btn-task" data-link-action="disconnect"></button>
            <button type="button" class="btn-init" data-link-action="connect">Connect</button>
            <button type="button" class="btn-clear" data-link-action="clear_fault">Clear fault</button>
        </div>
        <div class="device-card-body"></div>
        <div class="device-live-toggle-row" hidden>
            <button type="button" class="link-btn device-live-toggle" aria-expanded="false">Show live data</button>
        </div>
        <div class="device-live" hidden>
            <div class="device-live-error" hidden></div>
            <div class="device-live-telemetry"></div>
            <div class="device-live-io"></div>
        </div>`;
    card.querySelector('.device-card-title').textContent = deviceId;
    card.querySelectorAll('[data-link-action]').forEach((btn) => {
        btn.addEventListener('click', () => deviceLinkAction(deviceId, btn.dataset.linkAction));
    });
    card.querySelector('.device-live-toggle').addEventListener('click', () => toggleDeviceLive(deviceId));
    return card;
}

/* ---- Devices: live data and I/O panel ---- */
// Polled only for open panels while the Devices tab is visible, so no device
// I/O happens for nobody.
const deviceLiveOpen = new Set();
const deviceLiveInFlight = new Set();

function toggleDeviceLive(deviceId) {
    const card = document.querySelector(`.device-card[data-device-id="${CSS.escape(deviceId)}"]`);
    if (!card) return;
    const open = !deviceLiveOpen.has(deviceId);
    if (open) deviceLiveOpen.add(deviceId); else deviceLiveOpen.delete(deviceId);
    card.querySelector('.device-live').hidden = !open;
    const toggle = card.querySelector('.device-live-toggle');
    toggle.textContent = open ? 'Hide live data' : 'Show live data';
    toggle.setAttribute('aria-expanded', String(open));
    if (open) refreshDeviceLive(deviceId);
}

function formatLiveValue(value) {
    if (value === null || value === undefined) return '—';
    if (typeof value === 'boolean') return value ? 'yes' : 'no';
    if (typeof value === 'number') {
        if (Number.isInteger(value)) return String(value);
        return (value !== 0 && Math.abs(value) < 0.01) ? value.toExponential(3) : String(+value.toFixed(4));
    }
    if (Array.isArray(value)) return value.map(formatLiveValue).join(', ');
    return String(value);
}

function refreshDeviceLive(deviceId) {
    if (deviceLiveInFlight.has(deviceId)) return;
    deviceLiveInFlight.add(deviceId);
    fetch(`/api/devices/${encodeURIComponent(deviceId)}/live`)
        .then(r => r.json())
        .then((live) => renderDeviceLive(deviceId, live))
        .catch(() => renderDeviceLive(deviceId, { telemetry: {}, io: [], error: 'Could not reach the server.' }))
        .finally(() => deviceLiveInFlight.delete(deviceId));
}

function renderDeviceLive(deviceId, live) {
    const card = document.querySelector(`.device-card[data-device-id="${CSS.escape(deviceId)}"]`);
    if (!card) return;
    const errorEl = card.querySelector('.device-live-error');
    errorEl.hidden = !live.error;
    errorEl.textContent = live.error || '';

    const telemetry = live.telemetry || {};
    const rows = Object.keys(telemetry).map((key) =>
        `<tr><td>${escapeHtml(key.replace(/_/g, ' '))}</td><td>${escapeHtml(formatLiveValue(telemetry[key]))}</td></tr>`);
    const telemetryHtml = rows.length ? `<table>${rows.join('')}</table>` : '';
    const telemetryEl = card.querySelector('.device-live-telemetry');
    if (telemetryEl.dataset.html !== telemetryHtml) {
        telemetryEl.innerHTML = telemetryHtml;
        telemetryEl.dataset.html = telemetryHtml;
    }

    // I/O points are built once per point list and then only re-classed, so
    // a toggle keeps focus while the values refresh.
    const points = Array.isArray(live.io) ? live.io : [];
    const ioEl = card.querySelector('.device-live-io');
    const signature = points.map((p) => `${p.name}:${p.kind}:${p.writable}`).join('|');
    if (ioEl.dataset.signature !== signature) {
        ioEl.dataset.signature = signature;
        ioEl.innerHTML = '';
        points.forEach((point) => {
            const el = document.createElement(point.writable ? 'button' : 'span');
            el.className = 'io-point' + (point.writable ? ' io-toggle' : '');
            el.dataset.point = point.name;
            el.title = `${point.name} (${point.kind})` + (point.writable ? ': click to switch' : '');
            el.innerHTML = `<span class="io-led"></span><span>${escapeHtml(point.name)}</span>`;
            if (point.writable) {
                el.type = 'button';
                el.addEventListener('click', () => writeDeviceIo(deviceId, point.name, !el.classList.contains('on')));
            }
            ioEl.appendChild(el);
        });
    }
    const status = normalizeStatus(latestControllerStatus);
    const device = latestDevices.find((d) => d.device_id === deviceId) || {};
    const writeLocked = status === 'BUSY' || status === 'INITIALIZING' || !!device.detached;
    points.forEach((point) => {
        const el = ioEl.querySelector(`[data-point="${CSS.escape(point.name)}"]`);
        if (!el) return;
        el.classList.toggle('on', !!point.value);
        if (point.writable) el.disabled = writeLocked;
    });
}

function writeDeviceIo(deviceId, name, value) {
    fetch(`/api/devices/${encodeURIComponent(deviceId)}/io`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, value }),
    })
        .then(r => r.json().then((data) => ({ ok: r.ok, data })))
        .then(({ ok, data }) => setDevicesMessage(data.message, !ok || data.success === false))
        .catch(() => setDevicesMessage(`Error writing ${deviceId}.${name}.`, true))
        .finally(() => refreshDeviceLive(deviceId));
}

function pollDeviceLive() {
    const tab = document.getElementById('tab-devices');
    if (!tab || !tab.classList.contains('active') || document.hidden) return;
    deviceLiveOpen.forEach(refreshDeviceLive);
}

// Cards are created once per device and patched in place, so their buttons
// keep focus and hover across the 1 Hz refresh.
function renderDeviceCards(devices) {
    const container = document.getElementById('robot-info-container');
    if (!container) return;
    if (devices.length === 0) {
        container.innerHTML = '<p class="empty-state">No devices configured.</p>';
        return;
    }
    const existing = new Map();
    container.querySelectorAll('.device-card').forEach((card) => existing.set(card.dataset.deviceId, card));
    if (existing.size === 0) container.innerHTML = '';

    const status = normalizeStatus(latestControllerStatus);
    const controllerLocked = status === 'BUSY' || status === 'INITIALIZING';

    devices.forEach((device, index) => {
        const id = device.device_id || 'device';
        let card = existing.get(id);
        if (!card) card = createDeviceCard(id);
        existing.delete(id);
        if (container.children[index] !== card) container.insertBefore(card, container.children[index] || null);

        card.classList.toggle('device-detached', !!device.detached);

        const badges = deviceBadgesHtml(device);
        const badgesEl = card.querySelector('.device-card-badges');
        if (badgesEl.dataset.html !== badges) {
            badgesEl.innerHTML = badges;
            badgesEl.dataset.html = badges;
        }
        const table = deviceTableHtml(device);
        const bodyEl = card.querySelector('.device-card-body');
        if (bodyEl.dataset.html !== table) {
            bodyEl.innerHTML = table;
            bodyEl.dataset.html = table;
        }

        const canLink = !!device.supports_reconnect;
        const busy = controllerLocked || device.in_transition || devicePending.has(id);
        const lockReason = controllerLocked ? `Locked while the controller is ${status}.`
            : (device.in_transition || devicePending.has(id)) ? 'A link change is in progress.' : '';

        const releaseBtn = card.querySelector('[data-link-action="disconnect"]');
        releaseBtn.textContent = device.supports_monitoring ? 'Release control' : 'Disconnect';
        releaseBtn.hidden = !canLink || !!device.detached;
        releaseBtn.disabled = busy;
        releaseBtn.title = lockReason;

        const connectBtn = card.querySelector('[data-link-action="connect"]');
        connectBtn.hidden = !canLink || !(device.detached || device.link === 'disconnected');
        connectBtn.disabled = busy;
        connectBtn.title = lockReason;

        const clearBtn = card.querySelector('[data-link-action="clear_fault"]');
        clearBtn.hidden = !device.faulted || !!device.detached || !device.connected;
        clearBtn.disabled = busy;
        clearBtn.title = lockReason;

        card.querySelector('.device-card-actions').hidden =
            releaseBtn.hidden && connectBtn.hidden && clearBtn.hidden;
        card.querySelector('.device-live-toggle-row').hidden = !device.has_live_data;
    });
    existing.forEach((card) => card.remove());
}

function deviceLinkConfirm(device, action) {
    const id = device.device_id;
    const lockNote = 'Production, HOME, SHIPMENT and CALIBRATION stay locked until it is connected again; manual actions still run.';
    if (action === 'disconnect' && device.supports_monitoring) {
        return ['Release control', `Release control of ${id}?\n\n`
            + 'This app stops commanding it and its motion pauses. Another client (web portal, script) '
            + 'can then take control; it must resume motion before moving the robot. '
            + `The custom view keeps receiving its position.\n\n${lockNote}`];
    }
    if (action === 'disconnect') {
        return ['Disconnect device', `Disconnect ${id} so another program can use it?\n\n${lockNote}`];
    }
    if (action === 'connect') {
        const extra = device.supports_monitoring
            ? '\n\nThis app takes control back: the robot activates and homes. Close any other control connection first and make sure the cell is clear.'
            : '';
        return ['Connect device', `Connect ${id}?${extra}`];
    }
    return ['Clear device fault', `Clear the fault on ${id}?`];
}

function deviceLinkAction(deviceId, action) {
    const device = latestDevices.find((d) => d.device_id === deviceId) || { device_id: deviceId };
    const [title, message] = deviceLinkConfirm(device, action);
    showConfirmModal(title, message, () => {
        devicePending.add(deviceId);
        renderDeviceCards(latestDevices);
        setDevicesMessage(`${deviceId}: ${action === 'clear_fault' ? 'clearing fault' : action + 'ing'}…`);
        fetch(`/api/devices/${encodeURIComponent(deviceId)}/${action}`, { method: 'POST' })
            .then(r => r.json().then((data) => ({ ok: r.ok, data })))
            .then(({ ok, data }) => setDevicesMessage(data.message || `${deviceId}: done.`, !ok || data.success === false))
            .catch(() => setDevicesMessage(`Error sending ${action} for ${deviceId}.`, true))
            .finally(() => {
                devicePending.delete(deviceId);
                loadRobotInfo();
            });
    });
}

function detachedDevicesWarning() {
    const detached = latestDevices.filter((d) => d.detached);
    if (detached.length === 0) return '';
    const names = detached.map((d) => d.device_id + (d.link === 'monitoring' ? ' (monitoring only)' : '')).join(', ');
    return `\n\nReleased: ${names}. Steps that command ${detached.length > 1 ? 'these devices' : 'it'} will fail.`;
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

// The per-day cycle counts grow with every cycle: refresh them now and then.
setInterval(() => {
    if (document.getElementById('tab-production').classList.contains('active')) {
        refreshProdHistory();
    }
}, 10000);

// Fallback polling: skipped while the event stream (live.js) delivers the same data.
setInterval(() => {
    if (Live.active) return;
    if (document.getElementById('tab-production').classList.contains('active')) {
        refreshProdMetrics();
    }
    if (document.getElementById('tab-variables').classList.contains('active')) {
        refreshContext(true);
    }
}, 1000);

/* ---- Live updates (server-sent events) ---- */
function isTabActive(name) {
    const tab = document.getElementById('tab-' + name);
    return !!(tab && tab.classList.contains('active'));
}

Live.on('status', applyStatus);
Live.on('devices', (devices) => renderDevices(Array.isArray(devices) ? devices : []));
Live.on('prod', (snapshot) => {
    if (isTabActive('production')) renderProdSnapshot(snapshot);
});
Live.on('context', (snapshot) => {
    if (isTabActive('variables') && !isContextUserInteracting()) renderContext(snapshot);
});
Live.on('notice', (notice) => showToast(notice.level, notice.message));
Live.on('workspace', (info) => {
    if (info && info.name && info.name !== currentWorkspace && !workspaceSwitching) window.location.reload();
});

/* ---- Toasts (faults, failed tasks, link changes) ---- */
function showToast(level, message) {
    const container = document.getElementById('toast-container');
    if (!container || !message) return;
    const toast = document.createElement('div');
    const kind = ['error', 'warning', 'info'].includes(level) ? level : 'info';
    toast.className = `toast toast-${kind}`;
    toast.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    toast.textContent = message;
    toast.title = 'Click to dismiss';
    const dismiss = () => toast.remove();
    toast.addEventListener('click', dismiss);
    container.appendChild(toast);
    while (container.children.length > 4) container.firstElementChild.remove();
    setTimeout(dismiss, kind === 'error' ? 12000 : 6000);
}

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
setInterval(() => { if (!Live.active) updateRobotStatus(); }, 100);
setInterval(() => { if (!Live.active) loadRobotInfo(); }, 1000);
setInterval(pollDeviceLive, 500);
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
loadSystemInfo();
initProdTimeline();
loadWorkspaces();
Live.connect();
