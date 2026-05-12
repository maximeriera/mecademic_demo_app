const stateDisplays = () => Array.from(document.querySelectorAll('.js-robot-state'));
const messageLog   = document.getElementById('message-log');
let latestProdContext = null;
let activeProdSubTab = 'metrics';

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
            stateDisplays().forEach((el) => {
                const compact = el.classList.contains('status-box-compact');
                el.textContent = data.status;
                el.className = 'status-box ' + (compact ? 'status-box-compact ' : '') + 'js-robot-state ' + data.status.toUpperCase();
            });
        })
        .catch(() => {
            stateDisplays().forEach((el) => {
                const compact = el.classList.contains('status-box-compact');
                el.textContent = 'COMMUNICATION ERROR';
                el.className = 'status-box ' + (compact ? 'status-box-compact ' : '') + 'js-robot-state FAULTED';
            });
        });
}

function setMessage(text) { messageLog.textContent = text || 'No recent command activity.'; }

function setProdActionMessage(text, isError = false) {
    const actionLog = document.getElementById('prod-action-log');
    if (!actionLog) return;
    actionLog.textContent = text || 'No recent production context action.';
    actionLog.className = 'message-log ' + (isError ? 'prod-locked' : 'prod-unlocked');
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
    fetch('/api/abort', { method: 'POST' })
        .then(r => r.json()).then(d => {
            setMessage(d.message);
            setProdActionMessage(d.message, !d.success);
        })
        .catch(() => {
            setMessage('Error sending abort command.');
            setProdActionMessage('Error sending abort command.', true);
        });
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

/* ---- Log viewer ---- */
function populateLogFileList() {
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
