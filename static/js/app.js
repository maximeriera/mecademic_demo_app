const stateDisplay = document.getElementById('robot-state');
const messageLog   = document.getElementById('message-log');

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
}

/* ---- Status polling ---- */
function updateRobotStatus() {
    fetch('/api/status')
        .then(r => r.json())
        .then(data => {
            stateDisplay.textContent = data.status;
            stateDisplay.className = 'status-box ' + data.status.toUpperCase();
        })
        .catch(() => {
            stateDisplay.textContent = 'COMMUNICATION ERROR';
            stateDisplay.className = 'status-box FAULTED';
        });
}

function setMessage(text) { messageLog.textContent = text || 'No recent command activity.'; }

/* ---- Task / control commands ---- */
function sendTask(task) {
    fetch(`/api/task/${task}`, { method: 'POST' })
        .then(r => r.json()).then(d => setMessage(d.message))
        .catch(() => setMessage('Error sending task command.'));
}
function sendStop() {
    fetch('/api/stop', { method: 'POST' })
        .then(r => r.json()).then(d => setMessage(d.message))
        .catch(() => setMessage('Error sending stop command.'));
}
function sendAbort() {
    fetch('/api/abort', { method: 'POST' })
        .then(r => r.json()).then(d => setMessage(d.message))
        .catch(() => setMessage('Error sending abort command.'));
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
function sendShutdown() {
    fetch('/api/shutdown', { method: 'POST' })
        .then(r => r.json()).then(d => setMessage(d.message))
        .catch(() => setMessage('Error sending shutdown command.'));
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

/* ---- Startup ---- */
setInterval(updateRobotStatus, 100);
setInterval(loadRobotInfo, 1000);
updateRobotStatus();
loadRobotInfo();
