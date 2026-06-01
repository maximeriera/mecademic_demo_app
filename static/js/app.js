const stateDisplays = () => Array.from(document.querySelectorAll('.js-robot-state'));
const messageLog   = document.getElementById('message-log');
let latestProdContext = null;
let activeProdSubTab = 'metrics';
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

/* ---- Startup ---- */
setInterval(updateRobotStatus, 100);
setInterval(loadRobotInfo, 1000);
updateRobotStatus();
loadRobotInfo();
refreshProdContext();
loadManualActions();
initRestoreDropzone();
ensureBackupRestoreDropdowns();
renderBackupRestoreSelectMenu('backup-robot-picker');
renderBackupRestoreSelectMenu('restore-robot-picker');
loadBackupRestoreRobots();
