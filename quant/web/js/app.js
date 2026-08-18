
const { createApp } = Vue;
createApp({
    data() {
        return {
            tab: 'watchlist',
            // watchlist
            watchlist: [], newCode: '', searchResults: [], showSearch: false,
            // market
            quotes: [], codesInput: '',
            time: '', timer: null,
            // analyze
            analyzeCode: '', analysis: null, analysisList: [], analyzing: false,
            analyzeChart: null,
            macdChart: null, kdjChart: null, bollChart: null,
            dmiChart: null, psyChart: null, biasChart: null,
            // strategies
            strategies: [], metrics: {},
            showEditor: false, editor: { name: '', buy_rule: '', sell_rule: '' }, editorId: '',
            toast: '',
            // agent
            agent: {
                running: false, lastRun: null,
                ai: { cash: 10000, market_value: 0, total_value: 10000, total_return: 0, return_pct: 0, positions: [], trade_count: 0 },
                rule: { cash: 10000, market_value: 0, total_value: 10000, total_return: 0, return_pct: 0, positions: [], trade_count: 0 },
                ai_history: [], rule_history: [],
            },
            agentTrades: [], agentLogs: [], agentChart: null, agentTimer: null,
            // daily scan
            dsStatus: { next_run: null, last_run: null, last_status: 'idle' },
            dsRunning: false, dsReport: { exists: false, markdown: '' },
            dsReportDates: [], dsSelectedDate: '', dsPollTimer: null,
            // yujie picks
            yjStatus: { next_run: null, last_run: null, last_status: 'idle' },
            yjPicks: [], yjRunning: false, yjPollTimer: null, yjTimer: null,
            yjParams: null, yjDefaults: null, showYjParams: false, yjSaving: false,
            yjSearchQ: '', yjSearchResults: [], showYjSearch: false,
            yjScoreItem: null, yjScoreScoring: false,
            yjSelected: null, yjKlineChart: null, yjKlineLoading: false,
            yjView: 'list', yjPage: 1, yjPageSize: 50, yjJumpPage: '',
        };
    },
    computed: {
        builtinStrategies() { return this.strategies.filter(s => s.builtin); },
        customStrategies() { return this.strategies.filter(s => !s.builtin); },
        dsReportHtml() { return this.mdToHtml(this.dsReport.markdown || ''); },
        dsParsedStats() {
            const md = this.dsReport.markdown || '';
            const stats = [];
            const num = (re) => { const m = md.match(re); return m ? parseInt(m[1]) : null; };
            const up = num(/上涨\s*(\d+)/), down = num(/下跌\s*(\d+)/);
            const zt = num(/涨停\s*\*?\*?(\d+)/), dt = num(/跌停\s*\*?\*?(\d+)/);
            const amt = num(/成交额[^\d]*\*?\*?(\d+)/);
            if (up != null) stats.push({ label: '上涨', val: up, color: '#ef5350' });
            if (down != null) stats.push({ label: '下跌', val: down, color: '#66bb6a' });
            if (zt != null) stats.push({ label: '涨停', val: zt, color: '#ff1744' });
            if (dt != null) stats.push({ label: '跌停', val: dt, color: '#00c853' });
            if (amt != null) stats.push({ label: '成交额(亿)', val: amt, color: '#ffd54f' });
            return stats;
        },
        yjDetailRows() {
            const d = (this.yjSelected && this.yjSelected.detail) || {};
            const r = [];
            const add = (label, k, hit, fmt) => {
                let v = d[k];
                if (fmt && (typeof v === 'number' || typeof v === 'boolean')) v = fmt(v);
                if (v == null || v === undefined) v = '-';
                r.push({ k, label, value: v, hit: !!hit });
            };
            const boolFmt = v => v ? '✓' : '-';
            add('现价', 'price', d.price > 0, v => v.toFixed(2));
            add('MA5', 'ma5', d.ma5 > 0 && d.price > d.ma5, v => v.toFixed(2));
            add('MA10', 'ma10', d.ma10 > 0 && d.price > d.ma10, v => v.toFixed(2));
            add('MA20', 'ma20', d.ma20 > 0 && d.price > d.ma20, v => v.toFixed(2));
            add('MA60', 'ma60', d.ma60 > 0 && d.price > d.ma60, v => v.toFixed(2));
            add('多线多头', 'bull_ma', !!d.bull_ma, boolFmt);
            add('MACD DIFF(12-26)', 'macd_dif', !!d.macd_golden || !!d.macd_near, v => v.toFixed(3));
            add('MACD DEA(9)', 'macd_dea', !!d.macd_golden || !!d.macd_near, v => v.toFixed(3));
            add('MACD 柱', 'macd_bar', !!d.macd_green, v => v.toFixed(3));
            add('MACD 金叉', 'macd_golden', !!d.macd_golden, boolFmt);
            add('MACD 即将金叉', 'macd_near', !!d.macd_near, boolFmt);
            add('MACD 绿柱缩短', 'macd_green', !!d.macd_green, boolFmt);
            add('RSI6', 'rsi6', !!d.rsi_golden, v => v.toFixed(1));
            add('RSI12', 'rsi12', !!d.rsi_golden, v => v.toFixed(1));
            add('RSI 金叉', 'rsi_golden', !!d.rsi_golden, boolFmt);
            add('MOS 低点(底背离)', 'mos_bottom', !!d.mos_bottom, boolFmt);
            add('MOS CL1', 'cl1', !!d.mos_bottom, v => v.toFixed(2));
            add('MOS CL2', 'cl2', !!d.mos_bottom, v => v.toFixed(2));
            add('MOS 绿柱缩短', 'mos_green', !!d.mos_green, boolFmt);
            add('突破+金叉', 'breakout', !!d.breakout, boolFmt);
            add('120日低位区', 'low_pos', !!d.low_pos, boolFmt);
            add('深回撤(距120日高)', 'drawdown', !!d.drawdown, boolFmt);
            return r;
        },
        yjPagedPicks() {
            const start = (this.yjPage - 1) * this.yjPageSize;
            return this.yjPicks.slice(start, start + this.yjPageSize);
        },
        yjTotalPages() {
            return Math.ceil(this.yjPicks.length / this.yjPageSize) || 1;
        },
        yjPageRange() {
            const total = this.yjTotalPages, cur = this.yjPage;
            const range = [];
            let s = Math.max(1, cur - 4), e = Math.min(total, s + 9);
            s = Math.max(1, e - 9);
            for (let i = s; i <= e; i++) range.push(i);
            return range;
        }
    },
    watch: {
        // tab 变化时同步 location.hash(用 pushState,不触发 hashchange)
        tab(newTab) {
            const hashTab = '#' + newTab;
            if (location.hash !== hashTab) {
                history.pushState(null, '', hashTab);
            }
        }
    },
    methods: {
        toastMsg(msg) { this.toast = msg; setTimeout(() => this.toast = '', 2500); },
        // 浏览器前进后退或外部改 hash 时,同步 tab + 触发对应 load
        onHashChange() {
            const VALID = ['watchlist', 'market', 'analyze', 'strategies', 'agent', 'dailyscan', 'yujie'];
            const hashTab = location.hash.slice(1);
            if (!VALID.includes(hashTab) || this.tab === hashTab) return;
            this.tab = hashTab;
            // 触发对应 tab 的初始化(模拟原 @click 内联调用)
            if (hashTab === 'market') this.loadQuotes();
            else if (hashTab === 'strategies') this.loadStrategies();
            else if (hashTab === 'agent') this.loadAgentStatus();
            else if (hashTab === 'dailyscan') this.loadDailyScan();
            else if (hashTab === 'yujie') { this.yjView = 'list'; this.yjPage = 1; this.loadYujie(); }
        },
        async searchStock(q) {
            if (!q || q.length < 2) { this.searchResults = []; this.showSearch = false; return; }
            try {
                const res = await fetch('/api/search?q=' + encodeURIComponent(q));
                const data = await res.json();
                this.searchResults = data.results || [];
                this.showSearch = this.searchResults.length > 0;
            } catch (e) { this.searchResults = []; }
        },
        pickSearchResult(item) {
            this.newCode = item.code;
            this.showSearch = false;
            this.searchResults = [];
        },
        pctClass(p) { return p == null ? 'flat' : (p > 0 ? 'up' : (p < 0 ? 'down' : 'flat')); },
        badgeFor(q) {
            let s = 0;
            if (q.ma5 != null && q.price > q.ma5) s++;
            if (q.macd_bull) s++;
            if (q.kdj_signal === '超卖') s++;
            if (q.kdj_signal === '超买') s--;
            if (s >= 2) return 'badge-buy';
            if (s <= -1) return 'badge-sell';
            return 'badge-neutral';
        },
        badgeText(q) {
            let s = 0;
            if (q.ma5 != null && q.price > q.ma5) s++;
            if (q.macd_bull) s++;
            if (q.kdj_signal === '超卖') s++;
            if (q.kdj_signal === '超买') s--;
            if (s >= 2) return '📈 关注';
            if (s <= -1) return '📉 观望';
            return '➡️ 中性';
        },
        indicatorName(k) {
            const map = {
                macd_diff: 'MACD DIFF', macd_dea: 'MACD DEA', macd_bar: 'MACD柱',
                k: 'KDJ K', d: 'KDJ D', j: 'KDJ J',
                boll_u: 'BOLL上轨', boll_m: 'BOLL中轨', boll_l: 'BOLL下轨',
                bbiboll_u: 'BBIBOLL上轨', bbiboll_m: 'BBIBOLL中轨', bbiboll_l: 'BBIBOLL下轨',
                ma5: 'MA5', ma10: 'MA10', ma20: 'MA20', ma60: 'MA60',
                psy: 'PSY', bias1: 'BIAS6', bias2: 'BIAS12', bias3: 'BIAS24',
                pdi: 'PDI', mdi: 'MDI', adx: 'ADX', sar: 'SAR', tower: '宝塔线'
            };
            return map[k] || k;
        },
        formatIndicator(key, v) {
            if (v == null) return '-';
            if (key === 'tower') return v > 0 ? '红' : (v < 0 ? '绿' : '平');
            if (typeof v === 'number') {
                if (key.startsWith('bias') || key === 'psy' || ['k','d','j','pdi','mdi','adx'].includes(key)) return v.toFixed(1);
                if (['macd_diff','macd_dea','macd_bar'].includes(key)) return v.toFixed(3);
                return v.toFixed(2);
            }
            return v;
        },
        modeLabel(m) { return { buy: '🟢 买入', sell: '🔴 卖出', hold: '⚪ 观望' }[m]; },
        paramName(k) {
            return { fast:'快线', slow:'慢线', signal:'信号', n:'周期', k1:'K平滑', d1:'D平滑', period:'周期', std:'倍数', short:'短阈值', long:'长阈值', m:'M' }[k] || k;
        },
        metricName(k) { return this.metrics[k] || k; },
        opText(op) {
            return { '>':'大于', '>=':'大于等于', '<':'小于', '<=':'小于等于', '==':'等于', 'is_true':'为真' }[op] || op;
        },

        // ---------- watchlist ----------
        async loadWatchlist() {
            try {
                const res = await fetch('/api/watchlist');
                const data = await res.json();
                this.watchlist = data.data || [];
            } catch (e) { console.error('加载自选股失败', e); }
        },
        async addWatch() {
            const code = this.newCode.trim().replace(/^[szsh]/i, '');
            if (!code) return;
            try {
                const res = await fetch('/api/watchlist?code=' + encodeURIComponent(code), { method: 'POST' });
                const data = await res.json();
                this.toastMsg(data.msg);
                this.newCode = '';
                this.loadWatchlist();
            } catch (e) { this.toastMsg('添加失败'); }
        },
        async removeWatch(code) {
            await fetch('/api/watchlist/' + code, { method: 'DELETE' });
            this.loadWatchlist();
        },

        // ---------- market ----------
        async loadQuotes() {
            let codes = this.codesInput.replace(/\s+/g, '').replace(/，/g, ',');
            if (!codes && this.watchlist.length) {
                codes = this.watchlist.map(w => w.code).join(',');
                this.codesInput = codes;
            }
            if (!codes) { this.toastMsg('自选股为空，请先添加自选股'); return; }
            try {
                const res = await fetch('/api/quotes?codes=' + encodeURIComponent(codes));
                const data = await res.json();
                this.quotes = data.data;
                this.time = data.time;
            } catch (e) { console.error('加载行情失败', e); }
        },
        loadQuotesWithWatchlist() {
            const codes = this.watchlist.map(w => w.code).join(',');
            if (codes) { this.codesInput = codes; this.loadQuotes(); }
            else this.toastMsg('自选股为空');
        },

        // ---------- analyze ----------
        async doAnalyze() {
            const code = this.analyzeCode.trim().replace(/^[szsh]/i, '');
            if (!code) return;
            this.analyzing = true;
            this.tab = 'analyze';
            try {
                await this.loadAnalysis(code);
            } catch (e) { console.error('分析失败', e); }
            this.analyzing = false;
        },
        async analyzeAll() {
            if (!this.watchlist.length) { this.toastMsg('自选股为空'); return; }
            this.analyzing = true; this.tab = 'analyze'; this.analysisList = [];
            for (const w of this.watchlist) {
                try {
                    const res = await fetch('/api/analyze/' + w.code);
                    const data = await res.json();
                    if (!data.error) {
                        this.analysisList.push({ code: w.code, name: data.realtime && data.realtime.name || w.name });
                        if (!this.analysis) this.analysis = this.normalizeAnalysis(data);
                    }
                } catch (e) {}
            }
            this.analyzing = false;
            if (!this.analysis) this.toastMsg('所有自选分析失败');
        },
        async loadAnalysis(code) {
            const res = await fetch('/api/analyze/' + code);
            const data = await res.json();
            if (data.error) { this.toastMsg(data.error); return; }
            this.analysis = this.normalizeAnalysis(data);
            if (!this.analysisList.find(a => a.code === code)) {
                this.analysisList.push({ code: code, name: data.realtime && data.realtime.name || code });
            }
            this.$nextTick(() => {
                this.renderAnalyzeChart();
                this.renderIndicatorCharts();
            });
        },
        async viewAnalysis(code) {
            this.analyzing = true;
            await this.loadAnalysis(code);
            this.analyzing = false;
        },
        normalizeAnalysis(data) {
            const r = data.realtime || {};
            return {
                verdict: data.verdict, verdictIcon: data.verdict_icon,
                verdictClass: data.verdict === '买入' ? 'buy' : (data.verdict === '卖出' ? 'sell' : 'hold'),
                realtime: r, summary: data.summary,
                currentPrice: r.price != null ? r.price.toFixed(2) : '-',
                indicators: data.indicators,
                buyReasons: data.buy_reasons || [],
                sellReasons: data.sell_reasons || [],
                holdReasons: data.hold_reasons || [],
                kline: data.kline || [],
                indicator_series: data.indicator_series || {},
            };
        },
        renderAnalyzeChart() {
            const el = document.getElementById('analyzeChart');
            if (!el || !this.analysis) return;
            if (!this.analyzeChart) {
                this.analyzeChart = echarts.init(el);
                if (!window.__echartsInstances) window.__echartsInstances = {};
                window.__echartsInstances['analyzeChart'] = this.analyzeChart;
            }
            const kl = this.analysis.kline;
            const dates = kl.map(d => d.date);
            const ohlc = kl.map(d => [d.open, d.close, d.low, d.high]);
            const volumes = kl.map((d, idx) => ({
                value: d.volume,
                itemStyle: { color: d.close >= d.open ? '#d32f2f' : '#2e7d32' }
            }));
            const closes = kl.map(d => d.close);
            const vol = kl.map(d => d.volume);
            const ma5 = closes.map((_, i) => {
                if (i < 4) return '-';
                const s = closes.slice(i - 4, i + 1);
                return +(s.reduce((a, b) => a + b, 0) / 5).toFixed(2);
            });
            const ma10 = closes.map((_, i) => {
                if (i < 9) return '-';
                const s = closes.slice(i - 9, i + 1);
                return +(s.reduce((a, b) => a + b, 0) / 10).toFixed(2);
            });
            this.analyzeChart.setOption({
                tooltip: {
                    trigger: 'axis', axisPointer: { type: 'cross' },
                    formatter: function (params) {
                        const p = params.find(x => x.seriesType === 'candlestick');
                        if (!p) return '';
                        const d = kl[p.dataIndex];
                        return `${d.date}<br>
                            开: ${d.open.toFixed(2)}  收: ${d.close.toFixed(2)}<br>
                            高: ${d.high.toFixed(2)}  低: ${d.low.toFixed(2)}<br>
                            量: ${d.volume.toLocaleString()}`;
                    }
                },
                grid: [
                    { left: 60, right: 20, top: 20, height: '55%' },
                    { left: 60, right: 20, top: '72%', height: '18%' }
                ],
                xAxis: [
                    { type: 'category', data: dates, boundaryGap: true, axisLine: { lineStyle: { color: '#ccc' } } },
                    { type: 'category', data: dates, axisLabel: { show: false }, axisLine: { lineStyle: { color: '#ccc' } } }
                ],
                yAxis: [
                    { type: 'value', scale: true, splitLine: { lineStyle: { color: '#f0f0f0' } } },
                    { type: 'value', axisLabel: { show: false }, splitLine: { show: false } }
                ],
                dataZoom: [{ type: 'inside', xAxisIndex: [0, 1], start: 50, end: 100 }],
                series: [
                    {
                        name: 'K线', type: 'candlestick', data: ohlc,
                        itemStyle: {
                            color: '#d32f2f', color0: '#2e7d32',
                            borderColor: '#d32f2f', borderColor0: '#2e7d32'
                        }
                    },
                    { name: 'MA5', type: 'line', data: ma5, smooth: true, symbol: 'none', lineStyle: { width: 1, color: '#f9a825' } },
                    { name: 'MA10', type: 'line', data: ma10, smooth: true, symbol: 'none', lineStyle: { width: 1, color: '#283593' } },
                    { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: volumes }
                ]
            }, true);
        },

        renderIndicatorCharts() {
            const s = this.analysis.indicator_series;
            if (!s || !s.dates) return;
            const dates = s.dates;

            // MACD: DIFF, DEA lines + BAR histogram
            this._renderChart('macdChart', 'macdChart', {
                tooltip: { trigger: 'axis' },
                legend: { data: ['DIFF', 'DEA', 'BAR'], top: 0, textStyle: { fontSize: 10 } },
                grid: { left: 50, right: 15, top: 30, bottom: 25 },
                xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 9 } },
                yAxis: { type: 'value', scale: true, splitLine: { lineStyle: { color: '#f0f0f0' } } },
                dataZoom: [{ type: 'inside', start: 50 }],
                series: [
                    { name: 'DIFF', type: 'line', data: s.macd_diff, symbol: 'none', lineStyle: { width: 1.5, color: '#1565c0' } },
                    { name: 'DEA', type: 'line', data: s.macd_dea, symbol: 'none', lineStyle: { width: 1.5, color: '#e65100' } },
                    { name: 'BAR', type: 'bar', data: (s.macd_bar || []).map(v => v == null ? null : { value: v, itemStyle: { color: v >= 0 ? '#d32f2f' : '#2e7d32' } }) },
                ]
            });

            // KDJ: K, D, J lines
            this._renderChart('kdjChart', 'kdjChart', {
                tooltip: { trigger: 'axis' },
                legend: { data: ['K', 'D', 'J'], top: 0, textStyle: { fontSize: 10 } },
                grid: { left: 50, right: 15, top: 30, bottom: 25 },
                xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 9 } },
                yAxis: { type: 'value', scale: true, splitLine: { lineStyle: { color: '#f0f0f0' } } },
                dataZoom: [{ type: 'inside', start: 50 }],
                series: [
                    { name: 'K', type: 'line', data: s.k, symbol: 'none', lineStyle: { width: 1.5, color: '#1565c0' } },
                    { name: 'D', type: 'line', data: s.d, symbol: 'none', lineStyle: { width: 1.5, color: '#e65100' } },
                    { name: 'J', type: 'line', data: s.j, symbol: 'none', lineStyle: { width: 1.5, color: '#6a1b9a' } },
                ]
            });

            // BOLL: upper, mid, lower + close
            const closes = (this.analysis.kline || []).map(d => d.close);
            this._renderChart('bollChart', 'bollChart', {
                tooltip: { trigger: 'axis' },
                legend: { data: ['收盘', 'UP', 'MID', 'LOW'], top: 0, textStyle: { fontSize: 10 } },
                grid: { left: 50, right: 15, top: 30, bottom: 25 },
                xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 9 } },
                yAxis: { type: 'value', scale: true, splitLine: { lineStyle: { color: '#f0f0f0' } } },
                dataZoom: [{ type: 'inside', start: 50 }],
                series: [
                    { name: '收盘', type: 'line', data: closes, symbol: 'none', lineStyle: { width: 1, color: '#333' } },
                    { name: 'UP', type: 'line', data: s.boll_u, symbol: 'none', lineStyle: { width: 1, color: '#d32f2f', type: 'dashed' } },
                    { name: 'MID', type: 'line', data: s.boll_m, symbol: 'none', lineStyle: { width: 1, color: '#888' } },
                    { name: 'LOW', type: 'line', data: s.boll_l, symbol: 'none', lineStyle: { width: 1, color: '#2e7d32', type: 'dashed' } },
                ]
            });

            // DMI: +DI, -DI, ADX
            this._renderChart('dmiChart', 'dmiChart', {
                tooltip: { trigger: 'axis' },
                legend: { data: ['+DI', '-DI', 'ADX'], top: 0, textStyle: { fontSize: 10 } },
                grid: { left: 50, right: 15, top: 30, bottom: 25 },
                xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 9 } },
                yAxis: { type: 'value', scale: true, splitLine: { lineStyle: { color: '#f0f0f0' } } },
                dataZoom: [{ type: 'inside', start: 50 }],
                series: [
                    { name: '+DI', type: 'line', data: s.pdi, symbol: 'none', lineStyle: { width: 1.5, color: '#d32f2f' } },
                    { name: '-DI', type: 'line', data: s.mdi, symbol: 'none', lineStyle: { width: 1.5, color: '#2e7d32' } },
                    { name: 'ADX', type: 'line', data: s.adx, symbol: 'none', lineStyle: { width: 1.5, color: '#6a1b9a' } },
                ]
            });

            // PSY
            this._renderChart('psyChart', 'psyChart', {
                tooltip: { trigger: 'axis' },
                legend: { data: ['PSY'], top: 0, textStyle: { fontSize: 10 } },
                grid: { left: 50, right: 15, top: 30, bottom: 25 },
                xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 9 } },
                yAxis: { type: 'value', min: 0, max: 100, splitLine: { lineStyle: { color: '#f0f0f0' } } },
                dataZoom: [{ type: 'inside', start: 50 }],
                series: [
                    { name: 'PSY', type: 'line', data: s.psy, symbol: 'none', areaStyle: { opacity: 0.1 }, lineStyle: { width: 1.5, color: '#3949ab' } },
                    { type: 'line', data: dates.map(() => 50), symbol: 'none', lineStyle: { width: 1, color: '#ccc', type: 'dashed' } },
                ]
            });

            // BIAS
            this._renderChart('biasChart', 'biasChart', {
                tooltip: { trigger: 'axis' },
                legend: { data: ['BIAS1', 'BIAS2', 'BIAS3'], top: 0, textStyle: { fontSize: 10 } },
                grid: { left: 50, right: 15, top: 30, bottom: 25 },
                xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 9 } },
                yAxis: { type: 'value', scale: true, splitLine: { lineStyle: { color: '#f0f0f0' } } },
                dataZoom: [{ type: 'inside', start: 50 }],
                series: [
                    { name: 'BIAS1', type: 'line', data: s.bias1, symbol: 'none', lineStyle: { width: 1.5, color: '#1565c0' } },
                    { name: 'BIAS2', type: 'line', data: s.bias2, symbol: 'none', lineStyle: { width: 1.5, color: '#e65100' } },
                    { name: 'BIAS3', type: 'line', data: s.bias3, symbol: 'none', lineStyle: { width: 1.5, color: '#6a1b9a' } },
                ]
            });
        },

        _renderChart(domId, chartProp, option) {
            const el = document.getElementById(domId);
            if (!el) return;
            if (!this[chartProp]) {
                this[chartProp] = echarts.init(el);
                // resize 时记录到全局队列，由 window.resize 监听统一调度
                if (!window.__echartsInstances) window.__echartsInstances = {};
                window.__echartsInstances[domId] = this[chartProp];
            }
            this[chartProp].setOption(option, true);
        },

        // ---------- strategies ----------
        async loadStrategies() {
            try {
                const res = await fetch('/api/strategies');
                const data = await res.json();
                this.strategies = data.strategies || [];
            } catch (e) { console.error('加载策略失败', e); }
            try {
                const m = await (await fetch('/api/strategy-metrics')).json();
                this.metrics = m.metrics || {};
            } catch (e) {}
        },
        async toggleStrategy(s) {
            s.enabled = !s.enabled;
            await this.saveStrategyRow(s);
            this.toastMsg((s.enabled ? '已开启 ' : '已关闭 ') + s.name);
        },
        async updateParam(s, key, val) {
            const num = parseFloat(val);
            if (isNaN(num)) return;
            if (!s.params) s.params = {};
            s.params[key] = num;
            if (s.param_defs && s.param_defs[key]) s.param_defs[key].value = num;
            await this.saveStrategyRow(s);
            this.toastMsg('已更新 ' + s.name + ' 参数 ' + key + '=' + num);
        },
        async saveStrategyRow(s) {
            // 内置策略只提交 id/enabled/params（param_defs/detail 是展示用，不提交）
            const body = s.builtin
                ? { id: s.id, enabled: s.enabled, params: s.params || {} }
                : s;
            await fetch('/api/strategies/' + s.id, {
                method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
            });
        },
        async deleteStrategy(s) {
            if (!confirm('确认删除策略「' + s.name + '」？')) return;
            const res = await fetch('/api/strategies/' + s.id, { method: 'DELETE' });
            const data = await res.json();
            this.toastMsg(data.msg);
            this.loadStrategies();
        },
        openRuleEditor(s) {
            this.editorId = s ? s.id : '';
            this.editor = s ? {
                name: s.name,
                buy_rule: s.buy_rule || (s.buy && s.buy.length ? s.buy.map(c => metricName(c.metric) + ' ' + opText(c.op) + ' ' + c.threshold).join(' 且 ') : ''),
                sell_rule: s.sell_rule || (s.sell && s.sell.length ? s.sell.map(c => metricName(c.metric) + ' ' + opText(c.op) + ' ' + c.threshold).join(' 且 ') : ''),
            } : { name: '', buy_rule: '', sell_rule: '' };
            this.showEditor = true;
        },
        async saveStrategy() {
            const payload = {
                id: this.editorId || undefined,
                name: this.editor.name.trim() || '未命名策略',
                type: 'custom', enabled: true,
                buy_rule: (this.editor.buy_rule || '').trim(),
                sell_rule: (this.editor.sell_rule || '').trim(),
            };
            if (this.editorId) {
                await fetch('/api/strategies/' + this.editorId, {
                    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
                });
            } else {
                await fetch('/api/strategies', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
                });
            }
            this.showEditor = false;
            this.toastMsg('策略已保存');
            await this.loadStrategies();
        },

        openAnalyze(code) {
            this.analyzeCode = code;
            this.tab = 'analyze';
            this.doAnalyze();
        },

        // ---------- agent ----------
        async loadAgentStatus() {
            try {
                const res = await fetch('/api/agent/status');
                const data = await res.json();
                if (data.running !== undefined) {
                    this.agent = data;
                    this.$nextTick(() => this.renderAgentChart());
                }
            } catch (e) { /* console.error('agent status fail', e); */ }
        },
        async loadAgentTrades() {
            try {
                const res = await fetch('/api/agent/trades?limit=20');
                const data = await res.json();
                this.agentTrades = data.trades || [];
            } catch (e) {}
        },
        async loadAgentLogs() {
            try {
                const res = await fetch('/api/agent/logs?limit=50');
                const data = await res.json();
                this.agentLogs = data.logs || [];
            } catch (e) {}
        },
        exportTrades() {
            window.open('/api/agent/trades-csv', '_blank');
        },
        async startAgent() {
            const res = await (await fetch('/api/agent/start', { method: 'POST' })).json();
            this.toastMsg(res.msg);
            this.loadAgentStatus();
        },
        async stopAgent() {
            const res = await (await fetch('/api/agent/stop', { method: 'POST' })).json();
            this.toastMsg(res.msg);
            this.loadAgentStatus();
        },
        async resetAgent() {
            if (!confirm('确认重置？所有持仓和交易记录将被清空，资金恢复为初始值。')) return;
            const res = await (await fetch('/api/agent/reset', { method: 'POST' })).json();
            this.toastMsg(res.msg);
            this.loadAgentStatus();
            this.loadAgentTrades();
        },
        renderAgentChart() {
            const el = document.getElementById('agentChart');
            if (!el) return;
            const aiData = this.agent.ai_history || [];
            const ruleData = this.agent.rule_history || [];
            if (!aiData.length && !ruleData.length) return;
            if (!this.agentChart) {
                this.agentChart = echarts.init(el);
                if (!window.__echartsInstances) window.__echartsInstances = {};
                window.__echartsInstances['agentChart'] = this.agentChart;
            }
            const times = aiData.map(d => d.t);
            this.agentChart.setOption({
                tooltip: { trigger: 'axis' },
                grid: { left: 60, right: 20, top: 20, bottom: 30 },
                xAxis: { type: 'category', data: times, axisLine: { lineStyle: { color: '#ccc' } } },
                yAxis: { type: 'value', scale: true, splitLine: { lineStyle: { color: '#f0f0f0' } } },
                series: [
                    { name: 'AI Agent', type: 'line', data: aiData.map(d => d.v), smooth: true,
                      lineStyle: { color: '#d32f2f', width: 2 }, itemStyle: { color: '#d32f2f' } },
                    { name: '规则 Agent', type: 'line', data: ruleData.map(d => d.v), smooth: true,
                      lineStyle: { color: '#1565c0', width: 2 }, itemStyle: { color: '#1565c0' } },
                ],
                legend: { data: ['AI Agent', '规则 Agent'], bottom: 0 },
            }, true);
        },
        // ===== 每日扫描 =====
        async loadDailyScan() {
            await Promise.all([this.loadDailyScanStatus(), this.loadDailyScanReports(), this.loadDailyScanReport()]);
        },
        async loadDailyScanStatus() {
            try {
                const r = await (await fetch('/api/daily-scan/status')).json();
                this.dsStatus = r;
                this.dsRunning = r.last_status === 'running';
            } catch (e) {}
        },
        async loadDailyScanReports() {
            try {
                const r = await (await fetch('/api/daily-scan/reports')).json();
                this.dsReportDates = r.dates || [];
            } catch (e) {}
        },
        async loadDailyScanReport() {
            try {
                const url = '/api/daily-scan/report' + (this.dsSelectedDate ? '?date=' + this.dsSelectedDate : '');
                const r = await (await fetch(url)).json();
                this.dsReport = r;
            } catch (e) {}
        },
        async runDailyScan() {
            try {
                await fetch('/api/daily-scan/run', { method: 'POST' });
                this.toastMsg('已触发扫描，后台执行中…');
                this.dsRunning = true;
                // 轮询状态直到完成
                if (this.dsPollTimer) clearInterval(this.dsPollTimer);
                this.dsPollTimer = setInterval(async () => {
                    await this.loadDailyScanStatus();
                    if (this.dsStatus.last_status !== 'running') {
                        clearInterval(this.dsPollTimer);
                        this.dsPollTimer = null;
                        this.dsRunning = false;
                        await this.loadDailyScanReport();
                        await this.loadDailyScanReports();
                        this.toastMsg('扫描完成，日报已更新');
                    }
                }, 5000);
            } catch (e) { this.toastMsg('触发失败'); }
        },
        // ===== 玉姐精选 =====
        yjParamLabel(gkey, key) {
            const map = {
                scope: { min_history_days: '最少历史天数', min_amount_yi: '最低成交额(亿)', exclude_sz_code: '剔除板块代码' },
                macd: { golden_score: '金叉分数', near_size: '即将金叉差值阈值', near_score: '即将金叉分数', green_shrink_score: '绿柱缩短分数' },
                mos: { bottom_score: '低点分数', green_shrink_score: '绿柱缩短分数' },
                breakout: { score: '突破分数', period: '突破回看周期(日)' },
                rsi: { score: '金叉分数', p1: '短线周期', p2: '长线周期' },
                bull_ma: { score: '多头分数', m1: '短期均线1', m2: '短期均线2', m3: '中期均线', m4: '长期均线' },
                low_pos: { score: '低位分数', period: '回看周期(日)', ratio: '低位比例' },
                drawdown: { score: '回撤分数', period: '回看周期(日)', threshold: '回撤阈值' },
            };
            return (map[gkey] || {})[key] || key;
        },
        async loadYujie() {
            await Promise.all([this.loadYujieStatus(), this.loadYujiePicks()]);
        },
        async loadYujieStatus() {
            try {
                const r = await (await fetch('/api/yujie/status')).json();
                this.yjStatus = r;
                this.yjRunning = r.last_status === 'running';
            } catch (e) {}
        },
        async loadYujiePicks() {
            try {
                const r = await (await fetch('/api/yujie/picks')).json();
                this.yjPicks = r.picks || [];
                this.yjPage = 1;
            } catch (e) {}
        },
        yjSelectStock(p) {
            this.yjSelected = p;
            this.loadYjKline(p.code);
        },
        yjViewDetail(p) {
            this.yjSelected = p;
            this.yjView = 'detail';
            this.$nextTick(() => this.loadYjKline(p.code));
        },
        yjBackToList() {
            this.yjView = 'list';
            this.yjSelected = null;
            if (this.yjKlineChart) { this.yjKlineChart.dispose(); this.yjKlineChart = null; }
        },
        yjGoPage(n) {
            if (n >= 1 && n <= this.yjTotalPages) { this.yjPage = n; window.scrollTo({ top: 0, behavior: 'smooth' }); }
        },
        yjDoJump() {
            const n = parseInt(this.yjJumpPage);
            if (n >= 1 && n <= this.yjTotalPages) this.yjGoPage(n);
            this.yjJumpPage = '';
        },
        async loadYjKline(code) {
            this.yjKlineLoading = false;
            try {
                const r = await (await fetch('/api/kline/' + code + '?days=120')).json();
                const kl = r.data || [];
                this.$nextTick(() => this.renderYjKline(kl));
            } catch (e) {}
        },
        renderYjKline(kl) {
            const el = document.getElementById('yjKlineChart');
            if (!el || !kl.length) return;
            if (!this.yjKlineChart) {
                this.yjKlineChart = echarts.init(el);
                if (!window.__echartsInstances) window.__echartsInstances = {};
                window.__echartsInstances['yjKlineChart'] = this.yjKlineChart;
            }
            const dates = kl.map(d => d.date);
            const ohlc = kl.map(d => [d.open, d.close, d.low, d.high]);
            const volumes = kl.map(d => ({
                value: d.volume,
                itemStyle: { color: d.close >= d.open ? '#d32f2f' : '#2e7d32' }
            }));
            const closes = kl.map(d => d.close);
            const ma5 = closes.map((_, i) => {
                if (i < 4) return '-';
                const s = closes.slice(i - 4, i + 1);
                return +(s.reduce((a, b) => a + b, 0) / 5).toFixed(2);
            });
            const ma10 = closes.map((_, i) => {
                if (i < 9) return '-';
                const s = closes.slice(i - 9, i + 1);
                return +(s.reduce((a, b) => a + b, 0) / 10).toFixed(2);
            });
            this.yjKlineChart.setOption({
                tooltip: {
                    trigger: 'axis', axisPointer: { type: 'cross' },
                    formatter: function (params) {
                        const p = params.find(x => x.seriesType === 'candlestick');
                        if (!p) return '';
                        const d = kl[p.dataIndex];
                        return `${d.date}<br>开: ${d.open.toFixed(2)}  收: ${d.close.toFixed(2)}<br>高: ${d.high.toFixed(2)}  低: ${d.low.toFixed(2)}<br>量: ${d.volume.toLocaleString()}`;
                    }
                },
                grid: [
                    { left: 50, right: 16, top: 20, height: '58%' },
                    { left: 50, right: 16, top: '76%', height: '14%' }
                ],
                xAxis: [
                    { type: 'category', data: dates, boundaryGap: true, axisLabel: { show: false }, axisLine: { lineStyle: { color: '#ccc' } } },
                    { type: 'category', data: dates, axisLabel: { show: false }, axisLine: { lineStyle: { color: '#ccc' } } }
                ],
                yAxis: [
                    { type: 'value', scale: true, splitLine: { lineStyle: { color: '#f0f0f0' } } },
                    { type: 'value', axisLabel: { show: false }, splitLine: { show: false } }
                ],
                dataZoom: [{ type: 'inside', xAxisIndex: [0, 1], start: 60, end: 100 }],
                series: [
                    {
                        name: 'K线', type: 'candlestick', data: ohlc,
                        itemStyle: {
                            color: '#d32f2f', color0: '#2e7d32',
                            borderColor: '#d32f2f', borderColor0: '#2e7d32'
                        }
                    },
                    { name: 'MA5', type: 'line', data: ma5, smooth: true, symbol: 'none', lineStyle: { width: 1, color: '#f9a825' } },
                    { name: 'MA10', type: 'line', data: ma10, smooth: true, symbol: 'none', lineStyle: { width: 1, color: '#283593' } },
                    { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: volumes }
                ]
            }, true);
        },
        yjSearchKeyup() {
            const q = this.yjSearchQ.trim();
            if (!q || q.length < 2) { this.yjSearchResults = []; this.showYjSearch = false; return; }
            fetch('/api/search?q=' + encodeURIComponent(q))
                .then(r => r.json())
                .then(d => {
                    this.yjSearchResults = (d.results || []).slice(0, 8);
                    this.showYjSearch = true;
                }).catch(() => { this.yjSearchResults = []; });
        },
        queryYjScoreByCode(item) {
            this.yjSearchQ = item.name + ' ' + item.code;
            this.showYjSearch = false;
            this.yjSearchResults = [];
            this.queryYjScore(1);
        },
        async queryYjScore(force) {
            const q = this.yjSearchQ.trim();
            if (!q || q.length < 2) return;
            this.yjScoreScoring = true;
            this.yjScoreItem = { ok: true, code: '', name: '', score: null };
            try {
                const r = await (await fetch('/api/yujie/score?q=' + encodeURIComponent(q))).json();
                this.yjScoreItem = r;
            } catch (e) {
                this.yjScoreItem = { ok: false, msg: '查询失败' };
            }
            this.yjScoreScoring = false;
        },
        async runYujieScan() {
            try {
                const r = await (await fetch('/api/yujie/run', { method: 'POST' })).json();
                this.toastMsg(r.message || '已触发扫描');
                if (r.started) {
                    this.yjRunning = true;
                    if (this.yjPollTimer) clearInterval(this.yjPollTimer);
                    this.yjPollTimer = setInterval(async () => {
                        await this.loadYujieStatus();
                        if (this.yjStatus.last_status !== 'running') {
                            clearInterval(this.yjPollTimer);
                            this.yjPollTimer = null;
                            this.yjRunning = false;
                            await this.loadYujiePicks();
                            this.toastMsg('玉姐精选扫描完成');
                        }
                    }, 5000);
                }
            } catch (e) { this.toastMsg('触发失败'); }
        },
        async openYjParams() {
            try {
                const r = await (await fetch('/api/yujie/params')).json();
                this.yjParams = r.params || {};
                this.yjDefaults = r.defaults || {};
                this.showYjParams = true;
            } catch (e) { this.toastMsg('参数加载失败'); }
        },
        async saveYjParams() {
            try {
                this.yjSaving = true;
                const r = await (await fetch('/api/yujie/params', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ params: this.yjParams }),
                })).json();
                this.yjSaving = false;
                if (r.ok) {
                    this.toastMsg('参数已保存，下次扫描生效');
                    this.showYjParams = false;
                } else {
                    this.toastMsg('保存失败');
                }
            } catch (e) { this.yjSaving = false; this.toastMsg('保存失败'); }
        },
        openAnalysis(code) {
            this.analyzeCode = code;
            this.tab = 'analyze';
            this.doAnalyze();
        },
        mdToHtml(md) {
            if (!md) return '';
            const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
            // 行内：**粗体**、`代码`、[文本](url)、链接裸 URL
            const inline = s => esc(s)
                .replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>')
                .replace(/`([^`]+?)`/g,'<code style="background:#2a2a2e;padding:1px 4px;border-radius:3px;font-family:monospace;">$1</code>')
                .replace(/\[([^\]]+?)\]\(([^)\s]+)\)/g,'<a href="$2" target="_blank" style="color:#7ec;">$1</a>')
                .replace(/(^|[\s(])((https?:\/\/)[^\s<)]+[^\s<).!?,;:'\])])/g,
                    '$1<a href="$2" target="_blank" style="color:#7ec;">$2</a>');
            const lines = md.split('\n');
            let html = '', inList = false, inTable = false, tableRows = [], inQuote = false, inCode = false, codeBuf = [];
            const flushList = () => { if (inList) { html += '</ul>'; inList = false; } };
            const flushQuote = () => { if (inQuote) { html += '</blockquote>'; inQuote = false; } };
            const flushCode = () => {
                if (inCode) {
                    html += '<pre style="background:#1e1e22;padding:10px;border-radius:4px;overflow:auto;margin:8px 0;"><code style="font-family:monospace;font-size:12px;">' + esc(codeBuf.join('\n')) + '</code></pre>';
                    inCode = false; codeBuf = [];
                }
            };
            const flushTable = () => {
                if (!tableRows.length) return;
                const rows = tableRows.map(r => r.split('|').map(c => c.trim()).filter(Boolean));
                if (rows.length >= 2) {
                    html += '<table style="width:100%;border-collapse:collapse;font-size:13px;margin:8px 0;">';
                    html += '<thead><tr>' + rows[0].map(c => `<th style="border:1px solid #444;padding:4px 8px;background:#2a2a2e;">${esc(c)}</th>`).join('') + '</tr></thead>';
                    for (let i = 2; i < rows.length; i++) {
                        html += '<tr>' + rows[i].map(c => `<td style="border:1px solid #444;padding:4px 8px;">${esc(c)}</td>`).join('') + '</tr>';
                    }
                    html += '</table>';
                }
                tableRows = [];
            };
            for (let line of lines) {
                // 代码块围栏
                if (line.trim().startsWith('```')) {
                    if (inCode) { flushCode(); }
                    else { flushList(); flushQuote(); flushTable(); inTable = false; inCode = true; codeBuf = []; }
                    continue;
                }
                if (inCode) { codeBuf.push(line); continue; }
                // 表格
                if (line.trim().startsWith('|')) {
                    if (inList) { html += '</ul>'; inList = false; }
                    if (inQuote) { flushQuote(); }
                    inTable = true; tableRows.push(line.trim()); continue;
                } else if (inTable) { flushTable(); inTable = false; }
                // 引用块
                if (/^>\s?/.test(line)) {
                    if (inList) { html += '</ul>'; inList = false; }
                    if (!inQuote) { html += '<blockquote style="border-left:3px solid #7ec;padding:4px 12px;color:#9ab;margin:6px 0;background:#1e1e22;">'; inQuote = true; }
                    html += `<p style="margin:2px 0;line-height:1.6;">${inline(line.replace(/^>\s?/,''))}</p>`;
                    continue;
                } else if (inQuote) { flushQuote(); }
                // 标题
                if (/^###\s/.test(line)) { flushList(); html += `<h3 style="margin:14px 0 6px;color:#7ec;">${inline(line.replace(/^###\s/,''))}</h3>`; }
                else if (/^##\s/.test(line)) { flushList(); html += `<h2 style="margin:16px 0 8px;color:#9e7ec;border-bottom:1px solid #444;padding-bottom:4px;">${inline(line.replace(/^##\s/,''))}</h2>`; }
                else if (/^#\s/.test(line)) { flushList(); html += `<h1 style="margin:18px 0 10px;color:#fff;">${inline(line.replace(/^#\s/,''))}</h1>`; }
                else if (/^\s*[-*]\s/.test(line)) { if (!inList) { html += '<ul style="margin:6px 0 6px 20px;">'; inList = true; } html += `<li>${inline(line.replace(/^\s*[-*]\s/,''))}</li>`; }
                else if (line.trim() === '---') { flushList(); html += '<hr style="border:none;border-top:1px solid #444;margin:12px 0;">'; }
                else if (line.trim() === '') { flushList(); }
                else { flushList(); html += `<p style="margin:6px 0;line-height:1.7;">${inline(line)}</p>`; }
            }
            flushList(); flushQuote(); flushCode();
            if (inTable) flushTable();
            return html;
        },
    },
    mounted() {
        // 启动时根据 hash 恢复 tab
        const VALID = ['watchlist', 'market', 'analyze', 'strategies', 'agent', 'dailyscan', 'yujie'];
        const hashTab = location.hash.slice(1);
        if (VALID.includes(hashTab)) this.tab = hashTab;
        // 监听浏览器前进后退/外部 hash 变化
        window.addEventListener('hashchange', this.onHashChange);

        this.loadWatchlist().then(() => this.loadQuotes());
        this.loadStrategies();
        this.loadAgentStatus();
        this.loadAgentTrades();
        this.loadAgentLogs();
        this.timer = setInterval(() => {
            if (this.tab === 'watchlist') this.loadWatchlist();
            if (this.tab === 'market') this.loadQuotes();
        }, 60000);
        // Agent tab 5秒轮询
        this.agentTimer = setInterval(() => {
            if (this.tab === 'agent') {
                this.loadAgentStatus();
                this.loadAgentTrades();
                this.loadAgentLogs();
            }
        }, 5000);
        // 玉姐精选 tab 30秒轮询状态
        this.yjTimer = setInterval(() => {
            if (this.tab === 'yujie') {
                this.loadYujieStatus();
                if (this.yjStatus.last_status !== 'running') this.loadYujiePicks();
            }
        }, 30000);
        // 全局 echarts resize:窗口缩放时所有图表自适应重绘
        window.__echartsResizeHandler = () => {
            const insts = window.__echartsInstances || {};
            Object.values(insts).forEach(c => { try { c.resize(); } catch (e) {} });
        };
        window.addEventListener('resize', window.__echartsResizeHandler);
    },
    beforeUnmount() {
        if (this.timer) clearInterval(this.timer);
        if (this.agentTimer) clearInterval(this.agentTimer);
        if (this.yjTimer) clearInterval(this.yjTimer);
        if (this.dsPollTimer) clearInterval(this.dsPollTimer);
        if (this.yjPollTimer) clearInterval(this.yjPollTimer);
        window.removeEventListener('hashchange', this.onHashChange);
        if (window.__echartsResizeHandler) {
            window.removeEventListener('resize', window.__echartsResizeHandler);
        }
        // 销毁所有 echarts 实例
        const insts = window.__echartsInstances || {};
        Object.values(insts).forEach(c => { try { c.dispose(); } catch (e) {} });
    }
}).mount('#app');
