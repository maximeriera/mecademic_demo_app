// plot3d.js — dependency-free 3D scatter + surface on a 2D <canvas>.
//
//   const camera = Plot3D.camera();                    // share it to rotate panels together
//   const plot = Plot3D.create(canvas, camera, {labels: {x: 'X', y: 'Y', z: 'Z'}});
//   plot.setAxes({x: [x0, x1], y: [y0, y1], z: [z0, z1]});
//   plot.setLabels({x: 'X', y: 'Y', z: 'Z'});         // e.g. when the z unit changes
//   plot.setColor(z => 'rgb(...)');
//   plot.setPoints([[x, y, z], ...]);                  // scan path + points, last one marked
//   plot.setSurface({x: [...], y: [...], z: [[...]]}); // mesh on grid nodes, null = hole
//   Plot3D.animate([plot, ...], camera);              // auto-rotates; drag to orbit
//
// Orthographic projection, painter's ordering: fine for a few thousand points.
(function (global) {
    'use strict';

    const AUTO_ROTATE_RAD_S = 0.25;
    const IDLE_AFTER_DRAG_MS = 4000;

    function niceTicks(min, max, count) {
        const span = max - min;
        if (!(span > 0)) return { values: [min], step: 1 };
        const raw = span / count;
        const mag = Math.pow(10, Math.floor(Math.log10(raw)));
        const norm = raw / mag;
        const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
        const values = [];
        for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-9; v += step) {
            values.push(+v.toFixed(10));
        }
        return { values, step };
    }

    function decimalsFor(step) {
        return Math.max(0, Math.min(4, -Math.floor(Math.log10(step))));
    }

    function camera() {
        return { yaw: -0.65, pitch: 0.6, dragging: false, idleUntil: 0 };
    }

    function attachDrag(canvas, cam) {
        canvas.style.touchAction = 'none';
        canvas.style.cursor = 'grab';
        let last = null;
        canvas.addEventListener('pointerdown', e => {
            last = [e.clientX, e.clientY];
            cam.dragging = true;
            canvas.style.cursor = 'grabbing';
            canvas.setPointerCapture(e.pointerId);
        });
        canvas.addEventListener('pointermove', e => {
            if (!last) return;
            cam.yaw += (e.clientX - last[0]) * 0.01;
            // Below ~15° the floor collapses and its tick labels pile up.
            cam.pitch = Math.max(0.25, Math.min(1.5, cam.pitch + (e.clientY - last[1]) * 0.01));
            last = [e.clientX, e.clientY];
        });
        const end = () => {
            last = null;
            cam.dragging = false;
            cam.idleUntil = performance.now() + IDLE_AFTER_DRAG_MS;
            canvas.style.cursor = 'grab';
        };
        canvas.addEventListener('pointerup', end);
        canvas.addEventListener('pointercancel', end);
    }

    function create(canvas, cam, options) {
        let labels = (options && options.labels) || { x: 'X', y: 'Y', z: 'Z' };
        const style = Object.assign({
            text: '#9aa39f',
            grid: 'rgba(255, 255, 255, 0.08)',
            edge: 'rgba(255, 255, 255, 0.22)',
            path: 'rgba(252, 253, 252, 0.16)',
            marker: '#FCFDFC',
        }, options && options.style);
        let axes = null;
        let color = () => '#5DEFBF';
        let points = [];
        let surface = null;
        let message = 'Waiting for data…';

        attachDrag(canvas, cam);

        function render() {
            const dpr = window.devicePixelRatio || 1;
            const w = canvas.clientWidth;
            const h = canvas.clientHeight;
            if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
                canvas.width = Math.round(w * dpr);
                canvas.height = Math.round(h * dpr);
            }
            const ctx = canvas.getContext('2d');
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, w, h);
            ctx.font = '11px Archivo, sans-serif';

            if (!axes || (!points.length && !surface)) {
                ctx.fillStyle = style.text;
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillText(message, w / 2, h / 2);
                if (!axes) return;
            }

            // Half-extents of the drawn box: X fixed, Y keeps the real aspect,
            // Z flattened to half the floor, whatever the z range is.
            const aspect = (axes.y[1] - axes.y[0]) / (axes.x[1] - axes.x[0]);
            const box = { x: 1, y: Math.max(0.3, Math.min(1.5, aspect)), z: 0.5 };
            const cosY = Math.cos(cam.yaw), sinY = Math.sin(cam.yaw);
            const cosP = Math.cos(cam.pitch), sinP = Math.sin(cam.pitch);
            const reach = Math.hypot(box.x, box.y);
            const scale = Math.min((w - 90) / (2 * reach), (h - 60) / (2 * (reach * sinP + box.z * cosP)));
            const cx = w / 2;
            const cy = h / 2;

            // World → [screen x, screen y, depth]; larger depth = farther away.
            const P = (x, y, z) => {
                const X = ((x - axes.x[0]) / (axes.x[1] - axes.x[0]) * 2 - 1) * box.x;
                const Y = ((y - axes.y[0]) / (axes.y[1] - axes.y[0]) * 2 - 1) * box.y;
                const Z = ((z - axes.z[0]) / (axes.z[1] - axes.z[0]) * 2 - 1) * box.z;
                const xr = X * cosY - Y * sinY;
                const yr = X * sinY + Y * cosY;
                return [cx + xr * scale, cy - (Z * cosP + yr * sinP) * scale, yr * cosP - Z * sinP];
            };
            const line = (a, b) => {
                ctx.beginPath();
                ctx.moveTo(a[0], a[1]);
                ctx.lineTo(b[0], b[1]);
                ctx.stroke();
            };

            const [x0, x1] = axes.x, [y0, y1] = axes.y, [z0, z1] = axes.z;
            const xTicks = niceTicks(x0, x1, 5);
            const yTicks = niceTicks(y0, y1, 4);
            const zTicks = niceTicks(z0, z1, 4);

            // Floor grid and box edges.
            ctx.lineWidth = 1;
            ctx.strokeStyle = style.grid;
            xTicks.values.forEach(v => line(P(v, y0, z0), P(v, y1, z0)));
            yTicks.values.forEach(v => line(P(x0, v, z0), P(x1, v, z0)));
            ctx.strokeStyle = style.edge;
            [y0, y1].forEach(y => [z0, z1].forEach(z => line(P(x0, y, z), P(x1, y, z))));
            [x0, x1].forEach(x => [z0, z1].forEach(z => line(P(x, y0, z), P(x, y1, z))));
            [x0, x1].forEach(x => [y0, y1].forEach(y => line(P(x, y, z0), P(x, y, z1))));

            // Tick labels on the floor edges nearest the viewer, pushed outward.
            const centre = P((x0 + x1) / 2, (y0 + y1) / 2, z0);
            const label = (pt, text, push) => {
                const dx = pt[0] - centre[0], dy = pt[1] - centre[1];
                const len = Math.hypot(dx, dy) || 1;
                ctx.fillText(text, pt[0] + (dx / len) * push, pt[1] + (dy / len) * push);
            };
            ctx.fillStyle = style.text;
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            const yEdge = P(x0, y0, z0)[2] + P(x1, y0, z0)[2] < P(x0, y1, z0)[2] + P(x1, y1, z0)[2] ? y0 : y1;
            const xEdge = P(x0, y0, z0)[2] + P(x0, y1, z0)[2] < P(x1, y0, z0)[2] + P(x1, y1, z0)[2] ? x0 : x1;
            xTicks.values.forEach(v => label(P(v, yEdge, z0), v.toFixed(decimalsFor(xTicks.step)), 14));
            yTicks.values.forEach(v => label(P(xEdge, v, z0), v.toFixed(decimalsFor(yTicks.step)), 14));
            // Titles sit past the tick labels, clear of the middle tick.
            label(P((x0 + x1) / 2, yEdge, z0), labels.x, 46);
            label(P(xEdge, (y0 + y1) / 2, z0), labels.y, 46);

            // Z ticks on the vertical edge that is leftmost on screen. Its foot is
            // a floor corner that already carries tick labels, so skip low ticks.
            const corners = [[x0, y0], [x1, y0], [x0, y1], [x1, y1]];
            const [zx, zy] = corners.reduce((a, b) => (P(a[0], a[1], z0)[0] <= P(b[0], b[1], z0)[0] ? a : b));
            ctx.textAlign = 'right';
            zTicks.values
                .filter(v => v >= z0 + 0.15 * (z1 - z0))
                .forEach(v => {
                    const p = P(zx, zy, v);
                    ctx.fillText(v.toFixed(decimalsFor(zTicks.step)), p[0] - 10, p[1]);
                });
            const zTop = P(zx, zy, z1);
            ctx.textAlign = 'left';
            ctx.fillText(labels.z, zTop[0] - 6, zTop[1] - 14);

            if (surface) drawSurface(ctx, P);
            if (points.length) drawPoints(ctx, P, z0);
        }

        function drawSurface(ctx, P) {
            const xs = surface.x, ys = surface.y, zs = surface.z;
            const quads = [];
            for (let j = 0; j + 1 < ys.length; j++) {
                for (let i = 0; i + 1 < xs.length; i++) {
                    const z = [zs[j][i], zs[j][i + 1], zs[j + 1][i + 1], zs[j + 1][i]];
                    if (z.some(v => v === null || v === undefined)) continue;
                    const p = [
                        P(xs[i], ys[j], z[0]), P(xs[i + 1], ys[j], z[1]),
                        P(xs[i + 1], ys[j + 1], z[2]), P(xs[i], ys[j + 1], z[3]),
                    ];
                    quads.push({ p, depth: (p[0][2] + p[1][2] + p[2][2] + p[3][2]) / 4, z: (z[0] + z[1] + z[2] + z[3]) / 4 });
                }
            }
            quads.sort((a, b) => b.depth - a.depth);
            ctx.lineWidth = 0.6;
            ctx.strokeStyle = 'rgba(12, 26, 15, 0.45)';
            quads.forEach(q => {
                ctx.fillStyle = color(q.z);
                ctx.beginPath();
                ctx.moveTo(q.p[0][0], q.p[0][1]);
                for (let k = 1; k < 4; k++) ctx.lineTo(q.p[k][0], q.p[k][1]);
                ctx.closePath();
                ctx.fill();
                ctx.stroke();
            });
        }

        function drawPoints(ctx, P, zFloor) {
            const projected = points.map(([x, y, z]) => ({ s: P(x, y, z), z }));

            ctx.strokeStyle = style.path;
            ctx.lineWidth = 1;
            ctx.beginPath();
            projected.forEach(({ s }, i) => (i ? ctx.lineTo(s[0], s[1]) : ctx.moveTo(s[0], s[1])));
            ctx.stroke();

            projected
                .slice()
                .sort((a, b) => b.s[2] - a.s[2])
                .forEach(({ s, z }) => {
                    ctx.fillStyle = color(z);
                    ctx.beginPath();
                    ctx.arc(s[0], s[1], 2.6, 0, Math.PI * 2);
                    ctx.fill();
                });

            // The sensor's current spot: a probe line down to the floor and a ring.
            const last = points[points.length - 1];
            const tip = projected[projected.length - 1].s;
            const foot = P(last[0], last[1], zFloor);
            ctx.strokeStyle = style.marker;
            ctx.setLineDash([3, 3]);
            ctx.beginPath();
            ctx.moveTo(foot[0], foot[1]);
            ctx.lineTo(tip[0], tip[1]);
            ctx.stroke();
            ctx.setLineDash([]);
            ctx.lineWidth = 2;
            ctx.beginPath();
            ctx.arc(tip[0], tip[1], 6, 0, Math.PI * 2);
            ctx.stroke();
        }

        return {
            setAxes(value) { axes = value; },
            setLabels(value) { labels = value; },
            setColor(fn) { color = fn; },
            setPoints(value) { points = value || []; },
            setSurface(value) { surface = value || null; },
            setMessage(text) { message = text; },
            render,
        };
    }

    function animate(plots, cam) {
        const still = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        let previous = performance.now();
        function frame(now) {
            const dt = Math.min(0.1, (now - previous) / 1000);
            previous = now;
            if (!still && !cam.dragging && now > cam.idleUntil) cam.yaw += dt * AUTO_ROTATE_RAD_S;
            plots.forEach(plot => plot.render());
            requestAnimationFrame(frame);
        }
        requestAnimationFrame(frame);
    }

    global.Plot3D = { camera, create, animate, niceTicks, decimalsFor };
})(window);
