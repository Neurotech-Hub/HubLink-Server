/**
 * Cost comparison calculator + lightweight SVG chart for .cost-comparison-section.
 */
(function () {
    const NODE_PRICE = 99;
    const GATEWAY_PRICE = 399;
    const RETRIEVAL_HOURS_PER_NODE = 12 / 60;
    const FREQ_PER_MONTH = {
        Hourly: (24 * 365) / 12,
        Daily: 365 / 12,
        Weekly: 52 / 12,
    };

    /** Base viewBox width; height is computed from `.cc-plot-surface` so the plot fills flex space (no letterboxing). */
    const VB_W = 640;
    const M = { L: 96, R: 28, T: 28, B: 62 };

    function measureViewBoxHeight(svg) {
        const shell = svg.closest('.cc-plot-surface');
        const w = shell ? shell.clientWidth : 0;
        const h = shell ? shell.clientHeight : 0;
        if (!shell || w < 12 || h < 12) {
            return Math.round((VB_W * 280) / 640);
        }
        return Math.max(200, Math.round((VB_W * h) / w));
    }

    function money(x) {
        return x.toLocaleString(undefined, {
            style: 'currency',
            currency: 'USD',
            maximumFractionDigits: 0,
        });
    }

    function model(state) {
        const gateways = state.nodes === 60 ? 2 : 1;
        const cloud = state.nodes === 60 ? 49 : 19;
        const upfront = state.nodes * NODE_PRICE + gateways * GATEWAY_PRICE;
        const manualMonthly =
            state.rate * RETRIEVAL_HOURS_PER_NODE * state.nodes * FREQ_PER_MONTH[state.freq];
        const delta = manualMonthly - cloud;
        const breakEvenMonths = delta > 0 ? upfront / delta : Infinity;
        const maxMonth = Math.max(12, Math.ceil(breakEvenMonths * 2));
        return {
            gateways,
            cloud,
            upfront,
            manualMonthly,
            breakEvenMonths,
            maxMonth,
        };
    }

    function cumulativeTech(manualMonthly, tMonths) {
        return manualMonthly * tMonths;
    }

    function cumulativeHub(upfront, cloud, tMonths) {
        return upfront + cloud * tMonths;
    }

    function nicePlotMax(v) {
        const p = Math.pow(10, Math.floor(Math.log10(Math.max(v, 1))));
        return Math.ceil(v / p / 2) * p * 2;
    }

    function buildXs(end) {
        const endInt = Math.max(1, Math.round(end));
        const CAP = 160;
        if (endInt < CAP) {
            return Array.from({ length: endInt + 1 }, (_, i) => i);
        }
        const out = [];
        for (let i = 0; i < CAP; i++) {
            out.push(Math.round((i * endInt) / (CAP - 1)));
        }
        return Array.from(new Set(out)).sort(function (a, b) {
            return a - b;
        });
    }

    function linePath(points) {
        return points.map((pt, i) => (i ? 'L' : 'M') + pt[0].toFixed(1) + ' ' + pt[1].toFixed(1)).join(' ');
    }

    function svgEl(name, attrs, text) {
        const n = document.createElementNS('http://www.w3.org/2000/svg', name);
        Object.keys(attrs).forEach(function (k) {
            n.setAttribute(k, attrs[k]);
        });
        if (text !== undefined && text !== '') n.textContent = text;
        return n;
    }

    /** Draw responsive inline SVG cumulative-cost plot into `svg`. */
    function renderCostPlot(svg, mk, scenario) {
        const vbH = measureViewBoxHeight(svg);
        const vbW = VB_W;

        svg.innerHTML = '';
        svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
        svg.setAttribute('viewBox', `0 0 ${vbW} ${vbH}`);
        svg.appendChild(svgEl('title', {}, 'Cumulative spend: technician retrieval versus Hublink'));
        svg.appendChild(svgEl('desc', {}, 'Estimated cumulative totals from calculator inputs'));

        const { hasCrossover, chartUseDays, horizonDaysVal, horizonMonths, breakEvenDays, breakEvenMonths } = scenario;

        const maxXT = chartUseDays ? horizonDaysVal : horizonMonths;
        const xs = buildXs(maxXT);
        const toMonths = chartUseDays
            ? function (u) {
                  return u / 30.4375;
              }
            : function (u) {
                  return u;
              };

        const techY = xs.map(function (x) {
            return cumulativeTech(mk.manualMonthly, toMonths(x));
        });
        const hubY = xs.map(function (x) {
            return cumulativeHub(mk.upfront, mk.cloud, toMonths(x));
        });

        const ymax = nicePlotMax(Math.max(...techY, ...hubY, 1));
        const innerW = vbW - M.L - M.R;
        const innerH = vbH - M.T - M.B;
        const xScale = innerW / maxXT;
        const yScale = innerH / ymax;

        function xPx(u) {
            return M.L + u * xScale;
        }
        function yPx(v) {
            return M.T + innerH - v * yScale;
        }

        for (let g = 0; g <= 4; g++) {
            const val = (ymax * g) / 4;
            const y = yPx(val);
            svg.appendChild(
                svgEl('line', {
                    x1: M.L,
                    y1: y,
                    x2: vbW - M.R,
                    y2: y,
                    class: 'cc-chart-grid',
                }),
            );
            svg.appendChild(
                svgEl(
                    'text',
                    {
                        x: M.L - 10,
                        y: y + 5,
                        'text-anchor': 'end',
                        class: 'cc-chart-tick cc-chart-tick-y',
                    },
                    money(val),
                ),
            );
        }

        const xTickCount = 5;
        for (let i = 0; i <= xTickCount; i++) {
            const xv = Math.round((maxXT * i) / xTickCount);
            const xp = xPx(xv);
            svg.appendChild(
                svgEl(
                    'text',
                    {
                        x: xp,
                        y: vbH - 28,
                        'text-anchor': 'middle',
                        class: 'cc-chart-tick cc-chart-tick-x',
                    },
                    String(xv),
                ),
            );
        }

        svg.appendChild(
            svgEl('line', {
                x1: M.L,
                y1: M.T + innerH,
                x2: vbW - M.R,
                y2: M.T + innerH,
                class: 'cc-chart-axis',
            }),
        );
        svg.appendChild(
            svgEl('line', {
                x1: M.L,
                y1: M.T,
                x2: M.L,
                y2: M.T + innerH,
                class: 'cc-chart-axis',
            }),
        );

        const xLabel = chartUseDays ? 'Days from start' : 'Months from start';
        svg.appendChild(
            svgEl(
                'text',
                {
                    x: M.L + innerW / 2,
                    y: vbH - 6,
                    'text-anchor': 'middle',
                    class: 'cc-chart-axis-label',
                },
                xLabel,
            ),
        );
        svg.appendChild(
            svgEl(
                'text',
                {
                    x: 22,
                    y: M.T + innerH / 2,
                    transform: `rotate(-90 22 ${M.T + innerH / 2})`,
                    'text-anchor': 'middle',
                    class: 'cc-chart-axis-label',
                },
                'Cumulative cost',
            ),
        );

        const techPts = xs.map(function (x, i) {
            return [xPx(x), yPx(techY[i])];
        });
        const hubPts = xs.map(function (x, i) {
            return [xPx(x), yPx(hubY[i])];
        });
        svg.appendChild(svgEl('path', { d: linePath(techPts), class: 'cc-chart-line cc-chart-line-tech' }));
        svg.appendChild(svgEl('path', { d: linePath(hubPts), class: 'cc-chart-line cc-chart-line-hub' }));

        if (hasCrossover) {
            const beX = chartUseDays ? breakEvenDays : breakEvenMonths;
            if (beX > 0 && beX <= maxXT) {
                const bx = xPx(beX);
                svg.appendChild(
                    svgEl('line', {
                        x1: bx,
                        y1: M.T,
                        x2: bx,
                        y2: M.T + innerH,
                        class: 'cc-chart-be-line',
                    }),
                );
                const label = 'BREAK EVEN';
                const rightHeavy = bx > vbW * 0.54;
                const labelAttrs = {
                    y: M.T + 18,
                    class: 'cc-chart-be-label',
                };
                if (rightHeavy) {
                    labelAttrs.x = Math.max(M.L + 4, bx - 8);
                    labelAttrs['text-anchor'] = 'end';
                } else {
                    labelAttrs.x = Math.min(bx + 8, vbW - M.R - 8);
                }
                svg.appendChild(svgEl('text', labelAttrs, label));
            }
        }
    }

    function init(root) {
        const plotSvg = root.querySelector('.cc-cost-plot');
        const elGateways = root.querySelector('[data-cc-out="gateways"]');
        const elCloud = root.querySelector('[data-cc-out="cloud"]');
        const elHardware = root.querySelector('[data-cc-out="hardware"]');
        const elManual = root.querySelector('[data-cc-out="manual"]');
        const elBreakEven = root.querySelector('[data-cc-out="breakEven"]');

        if (!elGateways || !plotSvg || !elBreakEven) return;

        const plotSurface = plotSvg.closest('.cc-plot-surface');
        let roFrame = false;
        if (typeof ResizeObserver !== 'undefined' && plotSurface) {
            new ResizeObserver(function () {
                if (roFrame) return;
                roFrame = true;
                requestAnimationFrame(function () {
                    roFrame = false;
                    update();
                });
            }).observe(plotSurface);
        }

        let state = { rate: 30, freq: 'Daily', nodes: 30 };

        function update() {
            const mk = model(state);
            const hasCrossover = Number.isFinite(mk.breakEvenMonths) && mk.breakEvenMonths > 0;
            const useDays = hasCrossover && mk.breakEvenMonths < 3;
            const breakEvenDays = mk.breakEvenMonths * 30.4375;
            const chartUseDays = hasCrossover && useDays;

            let horizonMonths;
            if (hasCrossover) {
                horizonMonths = Math.min(48, Math.max(12, Math.ceil(mk.breakEvenMonths * 2)));
            } else {
                horizonMonths = 24;
            }

            let horizonDaysVal;
            if (chartUseDays) {
                horizonDaysVal = Math.min(365, Math.max(60, Math.ceil(breakEvenDays * 2)));
            }

            const breakEvenDisplay = !hasCrossover
                ? '—'
                : useDays
                  ? `${breakEvenDays.toFixed(0)} days`
                  : `${mk.breakEvenMonths.toFixed(1)} months`;

            elGateways.textContent = mk.gateways;
            elCloud.textContent = money(mk.cloud);
            elHardware.textContent = money(mk.upfront);
            elManual.textContent = money(mk.manualMonthly);
            elBreakEven.textContent = breakEvenDisplay;

            renderCostPlot(plotSvg, mk, {
                hasCrossover,
                chartUseDays,
                horizonDaysVal,
                horizonMonths,
                breakEvenDays,
                breakEvenMonths: mk.breakEvenMonths,
            });
        }

        root.querySelectorAll('[data-cc-control]').forEach(function (group) {
            group.addEventListener('click', function (e) {
                const btn = e.target.closest('button[data-value]');
                if (!btn || !group.contains(btn)) return;
                group.querySelectorAll('button').forEach((b) => b.classList.remove('active'));
                btn.classList.add('active');
                const c = group.getAttribute('data-cc-control');
                const v = btn.getAttribute('data-value');
                if (c === 'rate') state.rate = parseInt(v, 10);
                if (c === 'freq') state.freq = v;
                if (c === 'nodes') state.nodes = parseInt(v, 10);
                update();
            });
        });

        update();
    }

    function boot() {
        document.querySelectorAll('.cost-comparison-section:not([data-cc-initialized])').forEach(function (root) {
            root.setAttribute('data-cc-initialized', '1');
            init(root);
        });
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
    else boot();
})();
