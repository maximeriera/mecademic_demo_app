const stateDisplays = () => Array.from(document.querySelectorAll('.js-robot-state'));
const messageLog   = document.getElementById('message-log');
let latestProdContext = null;
let activeProdSubTab = 'metrics';
let activeConfigSubTab = 'devices';
let logDropdownInitialized = false;
let backupRestoreDropdownsInitialized = false;
let backupRestoreRobots = [];
let restoreArchiveFile = null;
let manualActions = [];
let latestControllerStatus = 'OFF';
let deviceConfig = { devices: {}, manual_actions: [], production_context_file: 'production_context.yaml' };
let deviceTypeCatalog = {};
let deviceTypeDropdownsInitialized = false;
let manualActionsConfig = [];
let contextConfigDraft = null;
let devReloadStatus = {
    enabled: false,
    can_reload: false,
    state: 'OFF',
    reason: 'Unknown',
};

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
    btn.classList.add('active');
    btn.setAttribute('aria-selected', 'true');
    if (name === 'logs') populateLogFileList();
    if (name === 'production') {
        refreshProdContext();
        switchProdSubTab(activeProdSubTab);
    }
    if (name === 'backup-restore') {
        loadBackupRestoreRobots();
    }
    if (name === 'manual') {
        loadManualActions();
    }
    if (name === 'config') {
        loadDeviceConfig();
        switchConfigSubTab(activeConfigSubTab);
    }
}

function switchConfigSubTab(name, btn) {
    activeConfigSubTab = name;

    document.querySelectorAll('#tab-config .prod-subtab-panel').forEach(panel => {
        panel.classList.remove('active');
        panel.hidden = true;
    });
    document.querySelectorAll('#tab-config .prod-subtab-btn').forEach(button => {
        button.classList.remove('active');
        button.setAttribute('aria-selected', 'false');
    });

    const activePanel = document.getElementById('config-subtab-' + name);
    if (activePanel) {
        activePanel.classList.add('active');
        activePanel.hidden = false;
    }

    const activeButton = btn || document.getElementById('config-subtab-btn-' + name);
    if (activeButton) {
        activeButton.classList.add('active');
        activeButton.setAttribute('aria-selected', 'true');
    }

    if (name === 'context') {
        loadContextConfig();
    }
    if (name === 'manual') {
        renderManualActionsConfig();
    }
}

function switchProdSubTab(name, btn) {
    activeProdSubTab = name;

    document.querySelectorAll('#tab-production .prod-subtab-panel').forEach(panel => {
        panel.classList.remove('active');
        panel.hidden = true;
    });
    document.querySelectorAll('#tab-production .prod-subtab-btn').forEach(button => {
        button.classList.remove('active');
        button.setAttribute('aria-selected', 'false');
    });

    const activePanel = document.getElementById('prod-subtab-' + name);
    if (activePanel) {
        activePanel.classList.add('active');
        activePanel.hidden = false;
    }

    const activeButton = btn || document.getElementById('prod-subtab-btn-' + name);
    if (activeButton) {
        activeButton.classList.add('active');
        activeButton.setAttribute('aria-selected', 'true');
    }
}

/* ---- Status polling ---- */
function updateRobotStatus() {
    fetch('/api/status')
        .then(r => r.json())
        .then(data => {
            latestControllerStatus = data.status;
            stateDisplays().forEach((el) => {
                const compact = el.classList.contains('status-box-compact');
                el.textContent = data.status;
                el.className = 'status-box ' + (compact ? 'status-box-compact ' : '') + 'js-robot-state ' + data.status.toUpperCase();
            });
            updateManualActionAvailability(data.status);
        })
        .catch(() => {
            latestControllerStatus = 'FAULTED';
            stateDisplays().forEach((el) => {
                const compact = el.classList.contains('status-box-compact');
                el.textContent = 'COMMUNICATION ERROR';
                el.className = 'status-box ' + (compact ? 'status-box-compact ' : '') + 'js-robot-state FAULTED';
            });
            updateManualActionAvailability('FAULTED');
        });
}

function setMessage(text) { messageLog.textContent = text || 'No recent command activity.'; }

function setProdActionMessage(text, isError = false) {
    const actionLog = document.getElementById('prod-action-log');
    if (!actionLog) return;
    actionLog.textContent = text || 'No recent production context action.';
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

function setDevicesConfigMessage(text, isError = false) {
    const log = document.getElementById('devices-config-log');
    if (!log) return;
    log.textContent = text || 'No device configuration action yet.';
    log.className = 'message-log ' + (isError ? 'prod-locked' : 'prod-unlocked');
}

function setContextConfigMessage(text, isError = false) {
    const log = document.getElementById('context-config-log');
    if (!log) return;
    log.textContent = text || 'No production context config action yet.';
    log.className = 'message-log ' + (isError ? 'prod-locked' : 'prod-unlocked');
}

function setManualConfigMessage(text, isError = false) {
    const log = document.getElementById('manual-config-log');
    if (!log) return;
    log.textContent = text || 'No manual action config action yet.';
    log.className = 'message-log ' + (isError ? 'prod-locked' : 'prod-unlocked');
}

function updateDevReloadStatusUi() {
    const statusEl = document.getElementById('dev-reload-status');
    const buttonEl = document.getElementById('btn-reload-custom-code');
    if (!statusEl || !buttonEl) return;

    if (!devReloadStatus.enabled) {
        statusEl.textContent = 'Dev reload mode disabled. Start app with --dev-reload or set MECADEMIC_DEV_RELOAD=1.';
        statusEl.className = 'dev-reload-status dev-reload-status-off';
        buttonEl.disabled = true;
        return;
    }

    if (devReloadStatus.can_reload) {
        statusEl.textContent = `Dev reload ready. Controller state: ${devReloadStatus.state}.`;
        statusEl.className = 'dev-reload-status dev-reload-status-ready';
        buttonEl.disabled = false;
        return;
    }

    statusEl.textContent = `Reload blocked: ${devReloadStatus.reason}`;
    statusEl.className = 'dev-reload-status dev-reload-status-blocked';
    buttonEl.disabled = true;
}

function refreshDevReloadStatus() {
    fetch('/api/dev/reload-status')
        .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
        .then(({ ok, data }) => {
            if (!ok || data.success === false) {
                throw new Error(data.message || 'Failed to load dev reload status.');
            }
            devReloadStatus = {
                enabled: !!data.enabled,
                can_reload: !!data.can_reload,
                state: data.state || 'OFF',
                reason: data.reason || 'unknown',
            };
            updateDevReloadStatusUi();
        })
        .catch(() => {
            devReloadStatus = {
                enabled: false,
                can_reload: false,
                state: 'FAULTED',
                reason: 'Dev reload status endpoint unavailable',
            };
            updateDevReloadStatusUi();
        });
}

function reloadCustomCode() {
    const buttonEl = document.getElementById('btn-reload-custom-code');
    if (buttonEl) {
        buttonEl.disabled = true;
    }

    fetch('/api/dev/reload-custom-code', { method: 'POST' })
        .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
        .then(({ ok, data }) => {
            const isError = !ok || data.success === false;
            const msg = data.message || (isError ? 'Custom code reload failed.' : 'Custom code reloaded.');
            setMessage(msg);
            setProdActionMessage(msg, isError);
            setManualActionMessage(msg, isError);
            refreshDevReloadStatus();
            if (!isError) {
                loadManualActions();
            }
        })
        .catch(() => {
            const msg = 'Error calling custom code reload endpoint.';
            setMessage(msg);
            setProdActionMessage(msg, true);
            setManualActionMessage(msg, true);
            refreshDevReloadStatus();
        });
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
            const container = document.getElementById('manual-actions-container');
            if (container) {
                container.innerHTML = `<p class="empty-state empty-state-error">${escapeHtml(err.message || 'Failed to load manual actions.')}</p>`;
            }
            setManualActionMessage(err.message || 'Failed to load manual actions.', true);
        });
}

function renderManualActions() {
    const container = document.getElementById('manual-actions-container');
    if (!container) return;

    if (!Array.isArray(manualActions) || manualActions.length === 0) {
        container.innerHTML = '<p class="empty-state">No manual actions configured for this project.</p>';
        return;
    }

    container.innerHTML = '';
    const grid = document.createElement('div');
    grid.className = 'manual-actions-grid';

    manualActions.forEach((action) => {
        const card = document.createElement('div');
        card.className = 'manual-action-card';

        const title = document.createElement('div');
        title.className = 'manual-action-title';
        title.textContent = action.label || action.key || 'Unnamed action';
        card.appendChild(title);

        const key = document.createElement('div');
        key.className = 'manual-action-key';
        key.textContent = action.key || '';
        card.appendChild(key);

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
        button.addEventListener('click', () => runManualAction(button));
        card.appendChild(button);

        grid.appendChild(card);
    });

    container.appendChild(grid);
    updateManualActionAvailability(latestControllerStatus);
}

function updateManualActionAvailability(status) {
    const buttons = document.querySelectorAll('.manual-action-run-btn');
    const normalizedStatus = normalizeStatus(status);
    const canRun = normalizedStatus === 'READY';
    const hint = document.getElementById('manual-state-hint');

    if (hint) {
        if (canRun) {
            hint.textContent = 'Controller READY. Manual actions can run.';
            hint.className = 'manual-state-hint manual-state-ready';
        } else {
            hint.textContent = `Controller ${status}. Manual actions are blocked until READY.`;
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

    showConfirmModal(confirmTitle, confirmMessage, () => {
        fetch(`/api/manual/actions/${encodeURIComponent(actionKey)}/run`, { method: 'POST' })
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

/* ---- Production context ---- */
function escapeHtml(text) {
    return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

function parseFieldValue(typeName, inputEl) {
    if (typeName === 'bool') return !!inputEl.checked;
    const raw = inputEl.value;
    if (typeName === 'int') return parseInt(raw, 10);
    if (typeName === 'float') return parseFloat(raw);
    return raw;
}

function isProdUserInteracting() {
    const active = document.activeElement;
    if (active && (active.closest('#prod-params-container') || active.closest('#prod-variables-container'))) {
        return true;
    }
    return false;
}

function captureProdScrollState() {
    const panel = document.getElementById('tab-production');
    return { panelScrollTop: panel ? panel.scrollTop : 0 };
}

function restoreProdScrollState(state) {
    const panel = document.getElementById('tab-production');
    if (panel) panel.scrollTop = state.panelScrollTop || 0;
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

function renderMetricCard(label, value, tone = '') {
    return `
        <div class="prod-metric-card ${tone}">
            <div class="prod-metric-label">${escapeHtml(label)}</div>
            <div class="prod-metric-value">${escapeHtml(String(value))}</div>
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

function renderProdMetrics(data) {
    const metrics = data.metrics || {};
    const recentCycles = metrics.cycle_history || [];
    const recentRuns = metrics.run_history || [];
    const metricsContainer = document.getElementById('prod-metrics-container');
    if (!metricsContainer) return;

    const cards = [
        renderMetricCard('Operational State', metrics.running ? 'RUNNING' : 'IDLE', metrics.running ? 'prod-metric-on' : 'prod-metric-off'),
        renderMetricCard('Completed Cycles', metrics.completed_cycles ?? 0),
        renderMetricCard('Total Part Count', metrics.part_count ?? 0),
        renderMetricCard('Current Run Time', formatDurationSeconds(metrics.elapsed_production_s)),
        renderMetricCard('Last Cycle Duration', formatDurationSeconds(metrics.last_cycle_duration_s)),
        renderMetricCard('Average Cycle Time', formatDurationSeconds(metrics.average_cycle_duration_s)),
        renderMetricCard('Cumulative Time', formatDurationSeconds(metrics.total_cycle_time_s)),
        renderMetricCard('Last Error State', metrics.last_error || 'NO ERRORS', metrics.last_error ? 'prod-metric-warn' : ''),
    ].join('');

    metricsContainer.innerHTML = `
        <div class="prod-metrics-grid">${cards}</div>
        <div class="prod-metric-history">
            <div class="prod-metric-history-title">Recent Cycles</div>
            ${renderRecentCycles(recentCycles)}
        </div>
        <div class="prod-metric-history">
            <div class="prod-metric-history-title">Recent Runs</div>
            ${renderRecentRuns(recentRuns)}
        </div>
    `;
}

function renderProdUnavailable(message) {
    const metricsContainer = document.getElementById('prod-metrics-container');
    const paramsContainer = document.getElementById('prod-params-container');
    const varsContainer = document.getElementById('prod-variables-container');

    if (metricsContainer) {
        metricsContainer.innerHTML = `
            <div class="prod-empty-panel">
                <div class="prod-empty-title">Production context unavailable</div>
                <div class="prod-empty-text">${escapeHtml(message || 'Unable to load production context right now.')}</div>
            </div>
        `;
    }

    if (paramsContainer) {
        paramsContainer.innerHTML = '<p class="empty-state">No production data available.</p>';
    }

    if (varsContainer) {
        varsContainer.innerHTML = '<p class="empty-state">No production data available.</p>';
    }
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

function renderProdContext(data) {
    const scrollState = captureProdScrollState();
    latestProdContext = data;
    const paramsContainer = document.getElementById('prod-params-container');
    const varsContainer = document.getElementById('prod-variables-container');
    const metricsContainer = document.getElementById('prod-metrics-container');

    const locked = !!data.locked;

    if (metricsContainer) {
        renderProdMetrics(data);
    }
    if (paramsContainer) {
        paramsContainer.innerHTML = renderNamespaceTable('params', data.params || {}, locked);
    }
    if (varsContainer) {
        varsContainer.innerHTML = renderNamespaceTable('variables', data.variables || {}, locked);
    }
    restoreProdScrollState(scrollState);
}

function renderNamespaceTable(namespace, entries, locked) {
    const keys = Object.keys(entries);
    if (keys.length === 0) {
        return '<p class="empty-state">No entries defined.</p>';
    }

    const cards = keys.map((key) => {
        const entry = entries[key];
        const editable = !locked && !!entry.editable_when_idle;
        const inputId = `prod-${namespace}-${key}-value`;
        const resetOn = Array.isArray(entry.reset_on) ? entry.reset_on.join(', ') : 'default policy';
        const scope = entry.persist_scope || 'default';

        let editorHtml = '';
        if (entry.type === 'bool') {
            editorHtml = `<input id="${inputId}" type="checkbox" ${entry.value ? 'checked' : ''} ${editable ? '' : 'disabled'}>`;
        } else {
            editorHtml = `<input id="${inputId}" type="text" value="${escapeHtml(entry.value)}" ${editable ? '' : 'disabled'}>`;
        }

        return `
            <div class="prod-card${editable ? ' prod-card-editable' : ' prod-card-locked'}">
                <div class="prod-card-header">
                    <span class="prod-card-key">${escapeHtml(key)}</span>
                    <span class="prod-card-type">${escapeHtml(entry.type)}</span>
                </div>
                <div class="prod-card-body">
                    <div class="prod-card-value">${editorHtml}</div>
                    <div class="prod-card-details">
                        <span><span class="prod-detail-label">Default:</span> ${escapeHtml(String(entry.default))}</span>
                        <span><span class="prod-detail-label">Reset:</span> ${escapeHtml(resetOn)}</span>
                        <span><span class="prod-detail-label">Scope:</span> ${escapeHtml(scope)}</span>
                        ${entry.description ? `<span class="prod-card-desc">${escapeHtml(entry.description)}</span>` : ''}
                    </div>
                    <div class="prod-card-actions">
                        <button type="button" class="btn-task" onclick="updateProdValue('${namespace}','${key}')" ${editable ? '' : 'disabled'}>Apply</button>
                        <button type="button" class="btn-shutdown" onclick="resetProdContext('${namespace}','${key}')" ${locked ? 'disabled' : ''}>Reset</button>
                    </div>
                </div>
            </div>
        `;
    }).join('');

    return `<div class="prod-cards">${cards}</div>`;
}

function refreshProdContext(auto = false) {
    if (auto && isProdUserInteracting()) {
        return;
    }
    fetch('/api/prod/context')
        .then(r => r.json())
        .then(data => renderProdContext(data))
        .catch(() => {
            if (!latestProdContext) {
                renderProdUnavailable('Failed to load production context.');
            }
            setProdActionMessage('Production context is temporarily unavailable.', true);
        });
}

function updateProdValue(namespace, key) {
    if (!latestProdContext || !latestProdContext[namespace] || !latestProdContext[namespace][key]) return;

    const entry = latestProdContext[namespace][key];
    const input = document.getElementById(`prod-${namespace}-${key}-value`);
    if (!input) return;

    const value = parseFieldValue(entry.type, input);
    if ((entry.type === 'int' || entry.type === 'float') && Number.isNaN(value)) {
        setMessage(`Invalid numeric value for ${namespace}.${key}.`);
        return;
    }

    const payload = { [namespace]: { [key]: { value } } };
    fetch('/api/prod/context', {
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
                setProdActionMessage(msg, true);
            } else {
                const msg = d.message || `${namespace}.${key} updated.`;
                setMessage(msg);
                setProdActionMessage(msg, false);
            }
            refreshProdContext();
        })
        .catch(() => {
            const msg = 'Error updating production context value.';
            setMessage(msg);
            setProdActionMessage(msg, true);
        });
}

function resetProdContext(namespace, key = null) {
    if (namespace === 'all' || !key) {
        showConfirmModal(
            "Confirm Reset All",
            "Are you sure you want to reset all production parameters and variables to their default values? This cannot be undone.",
            () => executeReset(namespace, key)
        );
    } else {
        executeReset(namespace, key);
    }
}

function executeReset(namespace, key) {
    const payload = { namespace };
    if (key) payload.key = key;

    fetch('/api/prod/context/reset', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    })
        .then(r => r.json())
        .then(d => {
            if (d.success === false) {
                const msg = d.message || 'Failed to reset production context.';
                setMessage(msg);
                setProdActionMessage(msg, true);
            } else {
                const msg = d.message || 'Production context reset.';
                setMessage(msg);
                setProdActionMessage(msg, false);
            }
            refreshProdContext();
        })
        .catch(() => {
            const msg = 'Error resetting production context.';
            setMessage(msg);
            setProdActionMessage(msg, true);
        });
}

/* ---- Device info cards ---- */
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

/* ---- Device configuration tab ---- */
function normalizeDeviceType(typeValue) {
    return String(typeValue || '').trim().toLowerCase();
}

function deviceResultElementId(deviceId) {
    return `device-test-result-${encodeURIComponent(deviceId)}`;
}

function getSortedDeviceTypes() {
    return Object.keys(deviceTypeCatalog).sort((a, b) => a.localeCompare(b));
}

function renderNewDeviceTypeOptions() {
    const select = document.getElementById('new-device-type');
    if (!select) return;

    const previous = select.value;
    const options = getSortedDeviceTypes();
    select.innerHTML = '';
    options.forEach((typeKey) => {
        const opt = document.createElement('option');
        opt.value = typeKey;
        opt.textContent = deviceTypeCatalog[typeKey].label || typeKey;
        if (previous && previous === typeKey) opt.selected = true;
        select.appendChild(opt);
    });
    if (!select.value && options.length > 0) {
        select.value = options[0];
    }
    const picker = document.getElementById('new-device-type-picker');
    if (picker) {
        renderDeviceTypePickerMenu(picker);
    }
}

function renderDeviceTypeSelect(selectedType) {
    const normalized = normalizeDeviceType(selectedType);
    const options = getSortedDeviceTypes().map((typeKey) => {
        const label = deviceTypeCatalog[typeKey].label || typeKey;
        return `<option value="${escapeHtml(typeKey)}" ${normalized === typeKey ? 'selected' : ''}>${escapeHtml(label)}</option>`;
    }).join('');
    return `
        <div class="log-select device-type-picker">
            <button type="button" class="log-select-trigger" aria-haspopup="listbox" aria-expanded="false">
                <span class="log-select-label">Select device type</span>
            </button>
            <div class="log-select-menu" role="listbox" aria-label="Device type selector"></div>
            <select class="device-type-select log-native-select" tabindex="-1" aria-hidden="true">${options}</select>
        </div>
    `;
}

function setDeviceTypePickerOpen(picker, isOpen) {
    if (!picker) return;
    const trigger = picker.querySelector('.log-select-trigger');
    if (!trigger) return;
    picker.classList.toggle('open', isOpen);
    trigger.setAttribute('aria-expanded', String(isOpen));
}

function updateDeviceTypePickerLabel(picker) {
    if (!picker) return;
    const sel = picker.querySelector('select');
    const label = picker.querySelector('.log-select-label');
    if (!sel || !label) return;

    const selected = sel.selectedOptions[0];
    label.textContent = selected ? (selected.textContent || 'Select device type') : 'Select device type';
}

function renderDeviceTypePickerMenu(picker, onChange = null) {
    if (!picker) return;
    const sel = picker.querySelector('select');
    const menu = picker.querySelector('.log-select-menu');
    if (!sel || !menu) return;

    menu.innerHTML = '';
    Array.from(sel.options).forEach((opt) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'log-select-option';
        btn.dataset.value = opt.value;
        btn.textContent = opt.textContent || '';
        btn.setAttribute('role', 'option');
        const active = opt.value === sel.value;
        btn.setAttribute('aria-selected', String(active));
        if (active) btn.classList.add('active');

        btn.addEventListener('click', () => {
            sel.value = opt.value;
            renderDeviceTypePickerMenu(picker, onChange);
            setDeviceTypePickerOpen(picker, false);
            if (onChange) onChange(opt.value);
        });
        menu.appendChild(btn);
    });

    updateDeviceTypePickerLabel(picker);
}

function initDeviceTypePicker(picker, onChange = null) {
    if (!picker || picker.dataset.initialized === 'true') {
        if (picker && onChange) picker.dataset.onchange = 'true';
        return;
    }
    const trigger = picker.querySelector('.log-select-trigger');
    if (!trigger) return;

    trigger.addEventListener('click', () => {
        const isOpen = picker.classList.contains('open');
        document.querySelectorAll('.device-type-picker.open').forEach((node) => {
            if (node !== picker) setDeviceTypePickerOpen(node, false);
        });
        setDeviceTypePickerOpen(picker, !isOpen);
    });

    renderDeviceTypePickerMenu(picker, onChange);
    picker.dataset.initialized = 'true';
}

function ensureDeviceTypeDropdowns() {
    if (deviceTypeDropdownsInitialized) return;

    document.addEventListener('click', (event) => {
        document.querySelectorAll('.device-type-picker').forEach((picker) => {
            if (picker.contains(event.target)) return;
            setDeviceTypePickerOpen(picker, false);
        });
    });

    document.addEventListener('keydown', (event) => {
        if (event.key !== 'Escape') return;
        document.querySelectorAll('.device-type-picker').forEach((picker) => {
            setDeviceTypePickerOpen(picker, false);
        });
    });

    deviceTypeDropdownsInitialized = true;
}

function renderDeviceFields(deviceId, cfg) {
    const typeKey = normalizeDeviceType(cfg.type);
    const schema = deviceTypeCatalog[typeKey];
    if (!schema) {
        return '<p class="empty-state empty-state-error">Unsupported device type.</p>';
    }

    const rows = (schema.fields || []).map((field) => {
        const fieldName = field.name;
        const value = Object.prototype.hasOwnProperty.call(cfg, fieldName)
            ? cfg[fieldName]
            : field.default;

        let inputHtml = '';
        if (field.type === 'bool') {
            inputHtml = `<input type="checkbox" class="device-field-input" data-field="${escapeHtml(fieldName)}" ${value ? 'checked' : ''}>`;
        } else {
            const inputType = (field.type === 'int' || field.type === 'float') ? 'number' : 'text';
            const step = field.type === 'float' ? 'step="any"' : '';
            inputHtml = `<input type="${inputType}" ${step} class="device-field-input" data-field="${escapeHtml(fieldName)}" value="${escapeHtml(value ?? '')}">`;
        }

        return `
            <div class="device-field-row">
                <label>${escapeHtml(field.label || fieldName)}${field.required ? ' *' : ''}</label>
                ${inputHtml}
            </div>
        `;
    }).join('');

    return rows || '<p class="empty-state">No extra fields for this device type.</p>';
}

function renderDeviceConfigCards() {
    const container = document.getElementById('devices-config-container');
    if (!container) return;

    const devices = deviceConfig.devices || {};
    const deviceIds = Object.keys(devices);
    if (deviceIds.length === 0) {
        container.innerHTML = '<p class="empty-state">No devices configured. Add one above.</p>';
        return;
    }

    container.innerHTML = deviceIds.map((deviceId) => {
        const cfg = devices[deviceId] || {};
        const encodedId = encodeURIComponent(deviceId);
        return `
            <div class="device-config-card" data-device-id="${escapeHtml(deviceId)}">
                <div class="device-config-header">
                    <div class="device-config-id">${escapeHtml(deviceId)}</div>
                    <button type="button" class="btn-shutdown" onclick="removeDeviceByEncoded('${encodedId}')">Remove</button>
                </div>
                <div class="device-config-body">
                    <div class="device-field-row">
                        <label>Type</label>
                        ${renderDeviceTypeSelect(cfg.type)}
                    </div>
                    <div class="device-fields-container">
                        ${renderDeviceFields(deviceId, cfg)}
                    </div>
                    <div class="devices-test-row">
                        <button type="button" class="btn-task" onclick="runDeviceControlByEncoded('${encodedId}','probe')">Probe</button>
                        <button type="button" class="btn-init" onclick="runDeviceControlByEncoded('${encodedId}','initialize')">Initialize</button>
                        <button type="button" class="btn-shutdown" onclick="runDeviceControlByEncoded('${encodedId}','shutdown')">Shutdown</button>
                        <button type="button" class="btn-clear" onclick="runDeviceControlByEncoded('${encodedId}','clear_fault')">Clear Fault</button>
                    </div>
                    <div class="device-test-result" id="${escapeHtml(deviceResultElementId(deviceId))}">No direct action executed yet.</div>
                </div>
            </div>
        `;
    }).join('');

    container.querySelectorAll('.device-config-card').forEach((card) => {
        const deviceId = card.dataset.deviceId;
        const typeSelect = card.querySelector('.device-type-select');
        const picker = card.querySelector('.device-type-picker');
        if (!typeSelect) return;

        initDeviceTypePicker(picker, () => {
            const cfg = deviceConfig.devices[deviceId];
            if (!cfg) return;
            cfg.type = normalizeDeviceType(typeSelect.value);
            const fieldsWrap = card.querySelector('.device-fields-container');
            if (fieldsWrap) {
                fieldsWrap.innerHTML = renderDeviceFields(deviceId, cfg);
            }
        });
    });
}

function loadDeviceConfig(notify = false) {
    fetch('/api/config')
        .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
        .then(({ ok, data }) => {
            if (!ok || data.success === false) {
                throw new Error(data.message || 'Failed to load config.');
            }
            deviceConfig = data.config || { devices: {} };
            manualActionsConfig = Array.isArray(deviceConfig.manual_actions) ? [...deviceConfig.manual_actions] : [];
            deviceTypeCatalog = data.device_type_catalog || {};
            renderNewDeviceTypeOptions();
            renderDeviceConfigCards();
            renderManualActionsConfig();
            initDeviceTypePicker(document.getElementById('new-device-type-picker'));
            if (notify) {
                setDevicesConfigMessage('Configuration refreshed.', false);
            }
        })
        .catch((err) => {
            const container = document.getElementById('devices-config-container');
            if (container) {
                container.innerHTML = `<p class="empty-state empty-state-error">${escapeHtml(err.message || 'Failed to load configuration.')}</p>`;
            }
            setDevicesConfigMessage(err.message || 'Failed to load configuration.', true);
        });
}

function readDeviceCardsToConfig() {
    const nextDevices = {};
    document.querySelectorAll('#devices-config-container .device-config-card').forEach((card) => {
        const deviceId = String(card.dataset.deviceId || '').trim();
        if (!deviceId) return;

        const typeSelect = card.querySelector('.device-type-select');
        const type = normalizeDeviceType(typeSelect?.value || '');
        const cfg = { type };

        card.querySelectorAll('.device-field-input').forEach((input) => {
            const field = input.dataset.field;
            if (!field) return;
            if (input.type === 'checkbox') {
                cfg[field] = !!input.checked;
            } else {
                cfg[field] = input.value;
            }
        });

        nextDevices[deviceId] = cfg;
    });

    return nextDevices;
}

function saveDeviceConfig() {
    const payload = { devices: readDeviceCardsToConfig() };
    fetch('/api/config/devices', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    })
        .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
        .then(({ ok, data }) => {
            if (!ok || data.success === false) {
                throw new Error(data.message || 'Failed to save config.');
            }
            setMessage(data.message || 'Configuration saved.');
            setDevicesConfigMessage(data.message || 'Configuration saved.', false);
            loadDeviceConfig();
            loadRobotInfo();
        })
        .catch((err) => {
            const msg = err.message || 'Failed to save config.';
            setMessage(msg);
            setDevicesConfigMessage(msg, true);
        });
}

function addDeviceFromForm() {
    const idInput = document.getElementById('new-device-id');
    const typeSelect = document.getElementById('new-device-type');
    if (!idInput || !typeSelect) return;

    const deviceId = String(idInput.value || '').trim();
    const deviceType = normalizeDeviceType(typeSelect.value);
    if (!deviceId) {
        setDevicesConfigMessage('Enter a device id before adding.', true);
        return;
    }
    if (Object.prototype.hasOwnProperty.call(deviceConfig.devices || {}, deviceId)) {
        setDevicesConfigMessage(`Device '${deviceId}' already exists.`, true);
        return;
    }
    if (!deviceTypeCatalog[deviceType]) {
        setDevicesConfigMessage('Select a supported device type.', true);
        return;
    }

    const newCfg = { type: deviceType };
    (deviceTypeCatalog[deviceType].fields || []).forEach((field) => {
        if (!field.required && !Object.prototype.hasOwnProperty.call(field, 'default')) {
            return;
        }
        if (Object.prototype.hasOwnProperty.call(field, 'default')) {
            newCfg[field.name] = field.default;
            return;
        }
        if (field.type === 'bool') newCfg[field.name] = false;
        else newCfg[field.name] = '';
    });

    if (!deviceConfig.devices) deviceConfig.devices = {};
    deviceConfig.devices[deviceId] = newCfg;
    idInput.value = '';
    renderDeviceConfigCards();
    setDevicesConfigMessage(`Device '${deviceId}' added (not saved yet).`, false);
}

function removeDevice(deviceId) {
    if (!deviceConfig.devices || !Object.prototype.hasOwnProperty.call(deviceConfig.devices, deviceId)) {
        return;
    }
    delete deviceConfig.devices[deviceId];
    renderDeviceConfigCards();
    setDevicesConfigMessage(`Device '${deviceId}' removed (not saved yet).`, false);
}

function removeDeviceByEncoded(encodedDeviceId) {
    removeDevice(decodeURIComponent(encodedDeviceId));
}

function runDeviceControlByEncoded(encodedDeviceId, action) {
    runDeviceControl(decodeURIComponent(encodedDeviceId), action);
}

function runDeviceControl(deviceId, action) {
    fetch(`/api/devices/${encodeURIComponent(deviceId)}/control`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action }),
    })
        .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
        .then(({ ok, data }) => {
            const target = document.getElementById(deviceResultElementId(deviceId));
            const msg = data.message || `Action '${action}' completed.`;
            if (!ok || data.success === false) {
                if (target) {
                    target.textContent = msg;
                    target.className = 'device-test-result device-test-result-error';
                }
                setDevicesConfigMessage(msg, true);
                return;
            }

            const result = data.result || {};
            const statusText = `connected=${result.connected} ready=${result.ready} faulted=${result.faulted}`;
            if (target) {
                target.textContent = `${msg} (${statusText})`;
                target.className = 'device-test-result device-test-result-ok';
            }
            setDevicesConfigMessage(`${msg} (${statusText})`, false);
            loadRobotInfo();
        })
        .catch(() => {
            const target = document.getElementById(deviceResultElementId(deviceId));
            const msg = `Action '${action}' failed for ${deviceId}.`;
            if (target) {
                target.textContent = msg;
                target.className = 'device-test-result device-test-result-error';
            }
            setDevicesConfigMessage(msg, true);
        });
}

function openConfigImportPicker() {
    const input = document.getElementById('device-config-import-input');
    if (input) input.click();
}

function importDeviceConfig(file) {
    if (!file) {
        setDevicesConfigMessage('Choose a .yaml/.yml file to import.', true);
        return;
    }
    const formData = new FormData();
    formData.append('config', file);

    fetch('/api/config/import', { method: 'POST', body: formData })
        .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
        .then(({ ok, data }) => {
            if (!ok || data.success === false) {
                throw new Error(data.message || 'Import failed.');
            }
            setDevicesConfigMessage(data.message || 'Configuration imported.', false);
            setMessage(data.message || 'Configuration imported.');
            loadDeviceConfig();
            loadRobotInfo();
        })
        .catch((err) => {
            setDevicesConfigMessage(err.message || 'Import failed.', true);
        });
}

function exportDeviceConfig() {
    window.location.href = '/api/config/export';
}

function exportGlobalConfig() {
    window.location.href = '/api/config/global/export';
}

function openGlobalConfigImportPicker() {
    const input = document.getElementById('global-config-import-input');
    if (input) input.click();
}

function importGlobalConfig(file) {
    if (!file) {
        setDevicesConfigMessage('Choose a .yaml/.yml file to import global config.', true);
        return;
    }
    const formData = new FormData();
    formData.append('config', file);

    fetch('/api/config/global/import', { method: 'POST', body: formData })
        .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
        .then(({ ok, data }) => {
            if (!ok || data.success === false) {
                throw new Error(data.message || 'Global config import failed.');
            }
            const msg = data.message || 'Global config imported.';
            setMessage(msg);
            setDevicesConfigMessage(msg, false);
            setManualConfigMessage(msg, false);
            setContextConfigMessage(msg, false);
            loadDeviceConfig();
            loadManualActions();
            loadContextConfig();
            loadRobotInfo();
            refreshProdContext();
        })
        .catch((err) => {
            const msg = err.message || 'Global config import failed.';
            setDevicesConfigMessage(msg, true);
        });
}

/* ---- Context config editor ---- */
function toContextEntryList(entriesObj) {
    if (!entriesObj || typeof entriesObj !== 'object') return [];
    return Object.entries(entriesObj).map(([key, entry]) => ({
        key,
        type: entry?.type || 'str',
        default: entry?.default,
        value: entry?.value,
        editable_when_idle: !!entry?.editable_when_idle,
        reset_on: entry?.reset_on,
        persist_scope: entry?.persist_scope || '',
        description: entry?.description || '',
    }));
}

function normalizeContextDraft(context) {
    const settings = (context && typeof context.settings === 'object') ? context.settings : {};
    return {
        version: Number.isFinite(Number(context?.version)) ? Number(context.version) : 1,
        settings: {
            storage_scope: String(settings.storage_scope || 'memory'),
            auto_persist: settings.auto_persist !== false,
            default_reset_events: settings.default_reset_events ?? 'none',
        },
        params: toContextEntryList(context?.params),
        variables: toContextEntryList(context?.variables),
    };
}

function formatResetOnForInput(value) {
    if (Array.isArray(value)) return value.join(', ');
    if (value === undefined || value === null) return '';
    return String(value);
}

function renderTypedValueInput(namespace, index, fieldName, typeName, value) {
    const safeValue = value ?? '';
    if (typeName === 'bool') {
        return `<input type="checkbox" ${safeValue ? 'checked' : ''} onchange="updateContextEntryBool('${namespace}', ${index}, '${fieldName}', this.checked)">`;
    }
    const inputType = (typeName === 'int' || typeName === 'float') ? 'number' : 'text';
    const step = typeName === 'float' ? ' step="any"' : '';
    return `<input type="${inputType}"${step} value="${escapeHtml(String(safeValue))}" oninput="updateContextEntryValue('${namespace}', ${index}, '${fieldName}', this.value)">`;
}

function renderContextSettingsEditor() {
    const container = document.getElementById('context-settings-container');
    if (!container || !contextConfigDraft) return;

    const settings = contextConfigDraft.settings;
    container.innerHTML = `
        <div class="device-config-card">
            <div class="device-config-body">
                <div class="device-field-row">
                    <label>Storage Scope</label>
                    <select onchange="updateContextSetting('storage_scope', this.value)">
                        <option value="memory" ${settings.storage_scope === 'memory' ? 'selected' : ''}>memory</option>
                        <option value="disk" ${settings.storage_scope === 'disk' ? 'selected' : ''}>disk</option>
                    </select>
                </div>
                <div class="device-field-row">
                    <label>Auto Persist</label>
                    <input type="checkbox" ${settings.auto_persist ? 'checked' : ''} onchange="updateContextSettingBool('auto_persist', this.checked)">
                </div>
                <div class="device-field-row">
                    <label>Default Reset Events</label>
                    <input type="text" value="${escapeHtml(formatResetOnForInput(settings.default_reset_events))}" placeholder="none or initialize,prod_start" oninput="updateContextSetting('default_reset_events', this.value)">
                </div>
            </div>
        </div>
    `;
}

function renderContextEntryCards(namespace) {
    const container = document.getElementById(`context-${namespace}-container`);
    if (!container || !contextConfigDraft) return;

    const entries = Array.isArray(contextConfigDraft[namespace]) ? contextConfigDraft[namespace] : [];
    if (entries.length === 0) {
        container.innerHTML = '<p class="empty-state">No entries configured.</p>';
        return;
    }

    container.innerHTML = entries.map((entry, index) => {
        const typeName = String(entry.type || 'str');
        return `
            <div class="device-config-card">
                <div class="device-config-header">
                    <div class="device-config-id">${escapeHtml(entry.key || `entry_${index + 1}`)}</div>
                    <button type="button" class="btn-shutdown" onclick="removeContextEntry('${namespace}', ${index})">Remove</button>
                </div>
                <div class="device-config-body">
                    <div class="device-field-row">
                        <label>Key</label>
                        <input type="text" value="${escapeHtml(entry.key || '')}" oninput="updateContextEntryValue('${namespace}', ${index}, 'key', this.value)">
                    </div>
                    <div class="device-field-row">
                        <label>Type</label>
                        <select onchange="updateContextEntryType('${namespace}', ${index}, this.value)">
                            <option value="int" ${typeName === 'int' ? 'selected' : ''}>int</option>
                            <option value="float" ${typeName === 'float' ? 'selected' : ''}>float</option>
                            <option value="bool" ${typeName === 'bool' ? 'selected' : ''}>bool</option>
                            <option value="str" ${typeName === 'str' ? 'selected' : ''}>str</option>
                        </select>
                    </div>
                    <div class="device-field-row">
                        <label>Default</label>
                        ${renderTypedValueInput(namespace, index, 'default', typeName, entry.default)}
                    </div>
                    <div class="device-field-row">
                        <label>Value</label>
                        ${renderTypedValueInput(namespace, index, 'value', typeName, entry.value)}
                    </div>
                    <div class="device-field-row">
                        <label>Editable When Idle</label>
                        <input type="checkbox" ${entry.editable_when_idle ? 'checked' : ''} onchange="updateContextEntryBool('${namespace}', ${index}, 'editable_when_idle', this.checked)">
                    </div>
                    <div class="device-field-row">
                        <label>Reset On</label>
                        <input type="text" value="${escapeHtml(formatResetOnForInput(entry.reset_on))}" placeholder="none or initialize,prod_start" oninput="updateContextEntryValue('${namespace}', ${index}, 'reset_on', this.value)">
                    </div>
                    <div class="device-field-row">
                        <label>Persist Scope</label>
                        <select onchange="updateContextEntryValue('${namespace}', ${index}, 'persist_scope', this.value)">
                            <option value="" ${!entry.persist_scope ? 'selected' : ''}>inherit settings</option>
                            <option value="memory" ${entry.persist_scope === 'memory' ? 'selected' : ''}>memory</option>
                            <option value="disk" ${entry.persist_scope === 'disk' ? 'selected' : ''}>disk</option>
                        </select>
                    </div>
                    <div class="device-field-row">
                        <label>Description</label>
                        <textarea class="context-entry-textarea" oninput="updateContextEntryValue('${namespace}', ${index}, 'description', this.value)">${escapeHtml(entry.description || '')}</textarea>
                    </div>
                </div>
            </div>
        `;
    }).join('');
}

function renderContextConfigEditor() {
    renderContextSettingsEditor();
    renderContextEntryCards('params');
    renderContextEntryCards('variables');
}

function updateContextSetting(field, value) {
    if (!contextConfigDraft) return;
    contextConfigDraft.settings[field] = value;
}

function updateContextSettingBool(field, value) {
    if (!contextConfigDraft) return;
    contextConfigDraft.settings[field] = !!value;
}

function updateContextEntryValue(namespace, index, field, value) {
    const entries = contextConfigDraft?.[namespace];
    if (!Array.isArray(entries) || !entries[index]) return;
    entries[index][field] = value;
}

function updateContextEntryBool(namespace, index, field, value) {
    const entries = contextConfigDraft?.[namespace];
    if (!Array.isArray(entries) || !entries[index]) return;
    entries[index][field] = !!value;
}

function updateContextEntryType(namespace, index, value) {
    const entries = contextConfigDraft?.[namespace];
    if (!Array.isArray(entries) || !entries[index]) return;
    entries[index].type = value;
    renderContextEntryCards(namespace);
}

function addContextEntry(namespace) {
    if (!contextConfigDraft || !Array.isArray(contextConfigDraft[namespace])) return;
    const keyInputId = namespace === 'params' ? 'new-context-param-key' : 'new-context-variable-key';
    const keyInput = document.getElementById(keyInputId);
    const key = String(keyInput?.value || '').trim();
    if (!key) {
        setContextConfigMessage(`Enter a ${namespace === 'params' ? 'param' : 'variable'} key before adding.`, true);
        return;
    }
    if (contextConfigDraft[namespace].some((entry) => String(entry.key || '').trim() === key)) {
        setContextConfigMessage(`Key '${key}' already exists in ${namespace}.`, true);
        return;
    }

    contextConfigDraft[namespace].push({
        key,
        type: 'str',
        default: '',
        value: '',
        editable_when_idle: true,
        reset_on: '',
        persist_scope: '',
        description: '',
    });
    if (keyInput) keyInput.value = '';
    renderContextEntryCards(namespace);
    setContextConfigMessage(`Added ${namespace.slice(0, -1)} '${key}' (not saved yet).`, false);
}

function removeContextEntry(namespace, index) {
    const entries = contextConfigDraft?.[namespace];
    if (!Array.isArray(entries) || !entries[index]) return;
    const key = entries[index].key || `${namespace.slice(0, -1)}_${index + 1}`;
    entries.splice(index, 1);
    renderContextEntryCards(namespace);
    setContextConfigMessage(`Removed ${namespace.slice(0, -1)} '${key}' (not saved yet).`, false);
}

function parseResetEvents(raw) {
    const text = String(raw ?? '').trim();
    if (!text) return undefined;
    if (text.toLowerCase() === 'none') return 'none';
    const parts = text.split(',').map((item) => item.trim()).filter(Boolean);
    return parts.length ? parts : 'none';
}

function coerceContextTypedValue(raw, typeName, fieldLabel) {
    if (typeName === 'bool') {
        if (typeof raw === 'boolean') return raw;
        const txt = String(raw ?? '').trim().toLowerCase();
        return ['1', 'true', 'yes', 'on'].includes(txt);
    }
    if (typeName === 'int') {
        const parsed = parseInt(String(raw ?? '').trim(), 10);
        if (Number.isNaN(parsed)) throw new Error(`${fieldLabel} must be an integer.`);
        return parsed;
    }
    if (typeName === 'float') {
        const parsed = parseFloat(String(raw ?? '').trim());
        if (Number.isNaN(parsed)) throw new Error(`${fieldLabel} must be a number.`);
        return parsed;
    }
    return String(raw ?? '');
}

function buildContextPayloadFromDraft() {
    if (!contextConfigDraft) {
        throw new Error('Context config is not loaded.');
    }

    const convertEntries = (entries, namespaceName) => {
        const out = {};
        (entries || []).forEach((entry, idx) => {
            const key = String(entry.key || '').trim();
            if (!key) {
                throw new Error(`${namespaceName} entry #${idx + 1} is missing a key.`);
            }
            if (Object.prototype.hasOwnProperty.call(out, key)) {
                throw new Error(`${namespaceName} key '${key}' is duplicated.`);
            }

            const typeName = String(entry.type || 'str');
            const normalized = {
                type: typeName,
                default: coerceContextTypedValue(entry.default, typeName, `${namespaceName}.${key}.default`),
                value: coerceContextTypedValue(entry.value, typeName, `${namespaceName}.${key}.value`),
                editable_when_idle: !!entry.editable_when_idle,
            };

            const parsedReset = parseResetEvents(entry.reset_on);
            if (parsedReset !== undefined) normalized.reset_on = parsedReset;

            const persistScope = String(entry.persist_scope || '').trim();
            if (persistScope) normalized.persist_scope = persistScope;

            const description = String(entry.description || '').trim();
            if (description) normalized.description = description;

            out[key] = normalized;
        });
        return out;
    };

    const defaultResetEvents = parseResetEvents(contextConfigDraft.settings.default_reset_events);
    const settings = {
        storage_scope: String(contextConfigDraft.settings.storage_scope || 'memory'),
        auto_persist: !!contextConfigDraft.settings.auto_persist,
    };
    if (defaultResetEvents !== undefined) {
        settings.default_reset_events = defaultResetEvents;
    }

    return {
        version: Number.isFinite(Number(contextConfigDraft.version)) ? Number(contextConfigDraft.version) : 1,
        settings,
        params: convertEntries(contextConfigDraft.params, 'params'),
        variables: convertEntries(contextConfigDraft.variables, 'variables'),
    };
}

function loadContextConfig(notify = false) {
    fetch('/api/config/context')
        .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
        .then(({ ok, data }) => {
            if (!ok || data.success === false) {
                throw new Error(data.message || 'Failed to load production context config.');
            }
            contextConfigDraft = normalizeContextDraft(data.context || {});
            renderContextConfigEditor();
            if (notify) {
                setContextConfigMessage('Production context config refreshed.', false);
            }
        })
        .catch((err) => {
            setContextConfigMessage(err.message || 'Failed to load production context config.', true);
        });
}

function saveContextConfig() {
    let contextPayload;
    try {
        contextPayload = buildContextPayloadFromDraft();
    } catch (validationErr) {
        setContextConfigMessage(validationErr.message || 'Invalid context config values.', true);
        return;
    }

    fetch('/api/config/context', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ context: contextPayload }),
    })
        .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
        .then(({ ok, data }) => {
            if (!ok || data.success === false) {
                throw new Error(data.message || 'Failed to save production context config.');
            }
            const msg = data.message || 'Production context config saved.';
            setMessage(msg);
            setContextConfigMessage(msg, false);
            refreshProdContext();
        })
        .catch((err) => {
            setContextConfigMessage(err.message || 'Failed to save production context config.', true);
        });
}

function exportContextConfig() {
    window.location.href = '/api/config/context/export';
}

function openContextConfigImportPicker() {
    const input = document.getElementById('context-config-import-input');
    if (input) input.click();
}

function importContextConfig(file) {
    if (!file) {
        setContextConfigMessage('Choose a .yaml/.yml file to import context config.', true);
        return;
    }
    const formData = new FormData();
    formData.append('context', file);

    fetch('/api/config/context/import', { method: 'POST', body: formData })
        .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
        .then(({ ok, data }) => {
            if (!ok || data.success === false) {
                throw new Error(data.message || 'Context config import failed.');
            }
            const msg = data.message || 'Production context imported.';
            setMessage(msg);
            setContextConfigMessage(msg, false);
            loadContextConfig();
            refreshProdContext();
        })
        .catch((err) => {
            setContextConfigMessage(err.message || 'Context config import failed.', true);
        });
}

/* ---- Manual actions config editor ---- */
function renderManualActionsConfig() {
    const container = document.getElementById('manual-actions-config-container');
    if (!container) return;

    if (!Array.isArray(manualActionsConfig) || manualActionsConfig.length === 0) {
        container.innerHTML = '<p class="empty-state">No manual actions configured.</p>';
        return;
    }

    container.innerHTML = '';
    const grid = document.createElement('div');
    grid.className = 'manual-actions-grid';

    manualActionsConfig.forEach((item, index) => {
        const card = document.createElement('div');
        card.className = 'manual-action-card';

        const key = document.createElement('div');
        key.className = 'manual-action-key';
        key.textContent = item.key || '';
        card.appendChild(key);

        const label = document.createElement('input');
        label.type = 'text';
        label.className = 'manual-config-input';
        label.value = item.label || '';
        label.placeholder = 'Label';
        label.addEventListener('input', () => {
            manualActionsConfig[index].label = label.value;
        });
        card.appendChild(label);

        const description = document.createElement('textarea');
        description.className = 'manual-config-textarea';
        description.value = item.description || '';
        description.placeholder = 'Description';
        description.addEventListener('input', () => {
            manualActionsConfig[index].description = description.value;
        });
        card.appendChild(description);

        const removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.className = 'btn-shutdown';
        removeBtn.textContent = 'Remove';
        removeBtn.addEventListener('click', () => {
            manualActionsConfig.splice(index, 1);
            renderManualActionsConfig();
        });
        card.appendChild(removeBtn);

        grid.appendChild(card);
    });

    container.appendChild(grid);
}

function addManualActionConfig() {
    const keyInput = document.getElementById('new-manual-action-key');
    const labelInput = document.getElementById('new-manual-action-label');
    if (!keyInput || !labelInput) return;

    const key = String(keyInput.value || '').trim();
    const label = String(labelInput.value || '').trim();
    if (!key) {
        setManualConfigMessage('Action key is required.', true);
        return;
    }
    if (manualActionsConfig.some((item) => item.key === key)) {
        setManualConfigMessage(`Action key '${key}' already exists.`, true);
        return;
    }

    manualActionsConfig.push({ key, label, description: '' });
    keyInput.value = '';
    labelInput.value = '';
    renderManualActionsConfig();
    setManualConfigMessage(`Manual action '${key}' added (not saved yet).`, false);
}

function saveManualActionsConfig() {
    fetch('/api/config/manual_actions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ manual_actions: manualActionsConfig }),
    })
        .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
        .then(({ ok, data }) => {
            if (!ok || data.success === false) {
                throw new Error(data.message || 'Failed to save manual actions config.');
            }
            const msg = data.message || 'Manual actions config saved.';
            setMessage(msg);
            setManualConfigMessage(msg, false);
            loadManualActions();
            loadDeviceConfig();
        })
        .catch((err) => {
            setManualConfigMessage(err.message || 'Failed to save manual actions config.', true);
        });
}

function exportManualActionsConfig() {
    window.location.href = '/api/config/manual_actions/export';
}

function openManualActionsImportPicker() {
    const input = document.getElementById('manual-actions-import-input');
    if (input) input.click();
}

function importManualActionsConfig(file) {
    if (!file) {
        setManualConfigMessage('Choose a .yaml/.yml file to import manual actions.', true);
        return;
    }
    const formData = new FormData();
    formData.append('manual_actions', file);

    fetch('/api/config/manual_actions/import', { method: 'POST', body: formData })
        .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
        .then(({ ok, data }) => {
            if (!ok || data.success === false) {
                throw new Error(data.message || 'Manual actions import failed.');
            }
            const msg = data.message || 'Manual actions imported.';
            setMessage(msg);
            setManualConfigMessage(msg, false);
            loadDeviceConfig();
            loadManualActions();
        })
        .catch((err) => {
            setManualConfigMessage(err.message || 'Manual actions import failed.', true);
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

function loadSelectedLog() {
    const sel = document.getElementById('log-file-select');
    const val = sel.value;
    const output = document.getElementById('log-output');
    if (!val) { output.innerText = 'Select a log file above.'; return; }
    fetch(`/api/logs/${val}?lines=300`)
        .then(r => r.json())
        .then(data => {
            if (data.message) { output.innerText = data.message; return; }
            output.innerHTML = data.lines.map(coloriseLine).join('');
            if (document.getElementById('log-autoscroll').checked) {
                output.scrollTop = output.scrollHeight;
            }
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
        refreshProdContext(true);
    }
}, 1000);

setInterval(refreshDevReloadStatus, 1500);

/* ---- Startup ---- */
setInterval(updateRobotStatus, 100);
setInterval(loadRobotInfo, 1000);
updateRobotStatus();
loadRobotInfo();
refreshProdContext();
loadManualActions();
loadDeviceConfig();
initRestoreDropzone();
ensureBackupRestoreDropdowns();
ensureDeviceTypeDropdowns();
renderBackupRestoreSelectMenu('backup-robot-picker');
renderBackupRestoreSelectMenu('restore-robot-picker');
loadBackupRestoreRobots();
refreshDevReloadStatus();

const configImportInput = document.getElementById('device-config-import-input');
if (configImportInput) {
    configImportInput.addEventListener('change', () => {
        const file = configImportInput.files && configImportInput.files.length > 0
            ? configImportInput.files[0]
            : null;
        importDeviceConfig(file);
        configImportInput.value = '';
    });
}

const globalConfigImportInput = document.getElementById('global-config-import-input');
if (globalConfigImportInput) {
    globalConfigImportInput.addEventListener('change', () => {
        const file = globalConfigImportInput.files && globalConfigImportInput.files.length > 0
            ? globalConfigImportInput.files[0]
            : null;
        importGlobalConfig(file);
        globalConfigImportInput.value = '';
    });
}

const contextConfigImportInput = document.getElementById('context-config-import-input');
if (contextConfigImportInput) {
    contextConfigImportInput.addEventListener('change', () => {
        const file = contextConfigImportInput.files && contextConfigImportInput.files.length > 0
            ? contextConfigImportInput.files[0]
            : null;
        importContextConfig(file);
        contextConfigImportInput.value = '';
    });
}

const manualActionsImportInput = document.getElementById('manual-actions-import-input');
if (manualActionsImportInput) {
    manualActionsImportInput.addEventListener('change', () => {
        const file = manualActionsImportInput.files && manualActionsImportInput.files.length > 0
            ? manualActionsImportInput.files[0]
            : null;
        importManualActionsConfig(file);
        manualActionsImportInput.value = '';
    });
}
