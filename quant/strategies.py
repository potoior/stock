import backtrader as bt


class BBI(bt.Indicator):
    """多空布林: BBI=(MA3+MA6+MA12+MA24)/4, 上下轨=BBI±m*std(n)"""

    lines = ("upper", "mid", "lower")
    params = (("m1", 3), ("m2", 6), ("m3", 12), ("m4", 24), ("n", 11), ("m", 2))

    def __init__(self):
        ma1 = bt.indicators.SMA(self.data.close, period=self.p.m1)
        ma2 = bt.indicators.SMA(self.data.close, period=self.p.m2)
        ma3 = bt.indicators.SMA(self.data.close, period=self.p.m3)
        ma4 = bt.indicators.SMA(self.data.close, period=self.p.m4)
        self.l.mid = (ma1 + ma2 + ma3 + ma4) / 4.0
        std = bt.indicators.StdDev(self.l.mid, period=self.p.n)
        self.l.upper = self.l.mid + std * self.p.m
        self.l.lower = self.l.mid - std * self.p.m


class BBIBOLLStrategy(bt.Strategy):
    """BBIBOLL多空布林：跌破下轨买入，突破上轨卖出，中轨上方做多"""

    params = (("printlog", False),)

    def __init__(self):
        self.bbiboll = BBI(self.data)
        self.order = None

    def log(self, txt):
        if self.params.printlog:
            dt = self.datas[0].datetime.date(0)
            print(f"{dt} {txt}")

    def notify_order(self, order):
        if order.status in [order.Completed, order.Canceled, order.Margin]:
            self.order = None

    def next(self):
        if self.order:
            return
        price = self.data.close[0]
        u = self.bbiboll.upper[0]
        m = self.bbiboll.mid[0]
        lo = self.bbiboll.lower[0]
        if not self.position:
            if price <= lo:
                size = int(self.broker.getcash() / price * 0.95)
                self.buy(size=size)
                self.log(f"BBIBOLL跌破下轨买入 {price:.2f}")
            elif price > m:
                size = int(self.broker.getcash() / price * 0.95)
                self.buy(size=size)
                self.log(f"BBIBOLL中轨上方买入 {price:.2f}")
        else:
            if price >= u:
                self.close()
                self.log(f"BBIBOLL突破上轨卖出 {price:.2f}")
            elif price < m:
                self.close()
                self.log(f"BBIBOLL跌破中轨卖出 {price:.2f}")


class TOWERStrategy(bt.Strategy):
    """宝塔线：翻红买入，翻绿卖出，持续方向持仓"""

    params = (("printlog", False),)

    def __init__(self):
        self.order = None
        self.prev_tower = 0

    def log(self, txt):
        if self.params.printlog:
            dt = self.datas[0].datetime.date(0)
            print(f"{dt} {txt}")

    def notify_order(self, order):
        if order.status in [order.Completed, order.Canceled, order.Margin]:
            self.order = None
            self.prev_tower = self.tower_now

    def next(self):
        if self.order:
            return
        if len(self.data) < 2:
            return
        price = self.data.close[0]
        if price > self.data.high[-1]:
            self.tower_now = 1
        elif price < self.data.low[-1]:
            self.tower_now = -1
        else:
            self.tower_now = self.prev_tower
        tw = self.tower_now
        pre = self.prev_tower
        if not self.position:
            if tw == 1 and pre != 1:
                size = int(self.broker.getcash() / price * 0.95)
                self.buy(size=size)
                self.log(f"宝塔线翻红买入 {price:.2f}")
        else:
            if tw == -1 and pre != -1:
                self.close()
                self.log(f"宝塔线翻绿卖出 {price:.2f}")


class MACDStrategy(bt.Strategy):
    """MACD三板斧：零上金叉/底背离抄底/顶背离逃顶/零下死叉卖"""

    params = (("fast", 12), ("slow", 26), ("signal", 9), ("lookback", 20), ("printlog", False))

    def __init__(self):
        self.macd = bt.indicators.MACD(
            self.data.close,
            period_me1=self.params.fast,
            period_me2=self.params.slow,
            period_signal=self.params.signal,
        )
        self.cross = bt.indicators.CrossOver(self.macd.macd, self.macd.signal)
        self.order = None
        self.last_golden_cross = -999

    def log(self, txt):
        if self.params.printlog:
            dt = self.datas[0].datetime.date(0)
            print(f"{dt} {txt}")

    def notify_order(self, order):
        if order.status in [order.Completed, order.Canceled, order.Margin]:
            self.order = None

    def next(self):
        if self.order:
            return
        if len(self.data) < self.params.lookback + 5:
            return
        price = self.data.close[0]
        diff = self.macd.macd[0]
        dea = self.macd.signal[0]
        zero = 0
        recent_low = min(self.data.close.get(size=self.params.lookback))
        recent_high = max(self.data.close.get(size=self.params.lookback))
        diff_low = min(self.macd.macd.get(size=self.params.lookback))
        diff_high = max(self.macd.macd.get(size=self.params.lookback))
        if not self.position:
            if self.cross[0] == 1:
                self.last_golden_cross = len(self.data)
                if diff > zero and dea > zero:
                    size = int(self.broker.getcash() / price * 0.95)
                    self.buy(size=size)
                    self.log(f"零上金叉买入(锦上添花) {price:.2f}")
                elif diff < zero and dea < zero:
                    prev_cross = 0
                    for i in range(-5, 0):
                        if (
                            self.macd.macd[i] > self.macd.signal[i]
                            and self.macd.macd[i - 1] < self.macd.signal[i - 1]
                        ):
                            prev_cross += 1
                    if prev_cross >= 1:
                        size = int(self.broker.getcash() / price * 0.95)
                        self.buy(size=size)
                        self.log(f"多次零下金叉买入(可靠) {price:.2f}")
                    else:
                        size = int(self.broker.getcash() / price * 0.95)
                        self.buy(size=size)
                        self.log(f"零下金叉买入(反弹) {price:.2f}")
                else:
                    size = int(self.broker.getcash() / price * 0.95)
                    self.buy(size=size)
                    self.log(f"金叉买入 {price:.2f}")
            elif price <= recent_low and diff > diff_low:
                size = int(self.broker.getcash() / price * 0.95)
                self.buy(size=size)
                self.log(f"底背离买入 {price:.2f}")
            elif diff > zero and dea > zero and price > self.data.close[-1] * 1.01:
                prev_low = min(self.data.close.get(size=self.params.lookback // 2))
                if prev_low >= self.data.close[-self.params.lookback]:
                    self.last_golden_cross = len(self.data)
                    size = int(self.broker.getcash() / price * 0.95)
                    self.buy(size=size)
                    self.log(f"主升浪买入 {price:.2f}")
        else:
            if self.cross[0] == -1:
                if diff < zero and dea < zero:
                    self.close()
                    self.log(f"零下死叉卖出(继续跌) {price:.2f}")
                elif diff > zero and dea > zero:
                    self.log(f"零上死叉(回调不卖) {price:.2f}")
                else:
                    self.close()
                    self.log(f"死叉卖出 {price:.2f}")
            elif price >= recent_high and diff <= diff_high and self.cross[0] == -1:
                self.close()
                self.log(f"顶背离死叉卖出 {price:.2f}")


class KDJStrategy(bt.Strategy):
    """KDJ超买超卖 + 金叉死叉 + 钝化识别"""

    params = (("period", 9), ("fast", 3), ("slow", 3), ("printlog", False))

    def __init__(self):
        self.k = bt.indicators.Stochastic(
            self.data, period=self.params.period, period_dfast=self.params.fast, period_dslow=self.params.slow
        )
        self.cross = bt.indicators.CrossOver(self.k.lines.percK, self.k.lines.percD)
        self.order = None

    def log(self, txt):
        if self.params.printlog:
            dt = self.datas[0].datetime.date(0)
            print(f"{dt} {txt}")

    def notify_order(self, order):
        if order.status in [order.Completed, order.Canceled, order.Margin]:
            self.order = None

    def next(self):
        if self.order:
            return
        k_val = self.k.lines.percK[0]
        d_val = self.k.lines.percD[0]
        if not self.position:
            if self.cross[0] == 1:
                if k_val < 10 and d_val < 20:
                    size = int(self.broker.getcash() / self.data.close[0] * 0.95)
                    self.buy(size=size)
                    self.log(f"KDJ超卖区金叉买入 K={k_val:.1f} D={d_val:.1f}")
                elif k_val < 20:
                    size = int(self.broker.getcash() / self.data.close[0] * 0.95)
                    self.buy(size=size)
                    self.log(f"KDJ金叉买入 K={k_val:.1f}")
        else:
            if self.cross[0] == -1:
                if k_val > 90 and d_val > 80:
                    self.close()
                    self.log(f"KDJ超买区死叉卖出 K={k_val:.1f} D={d_val:.1f}")
                elif k_val > 80:
                    self.close()
                    self.log(f"KDJ死叉卖出 K={k_val:.1f}")


class MAStopStrategy(bt.Strategy):
    """5日均线止损：站上买入，跌破卖出"""

    params = (("ma_period", 5), ("printlog", False))

    def __init__(self):
        self.ma = bt.indicators.SMA(self.data.close, period=self.params.ma_period)
        self.order = None

    def log(self, txt):
        if self.params.printlog:
            dt = self.datas[0].datetime.date(0)
            print(f"{dt} {txt}")

    def notify_order(self, order):
        if order.status in [order.Completed, order.Canceled, order.Margin]:
            self.order = None

    def next(self):
        if self.order:
            return
        price = self.data.close[0]
        ma_val = self.ma[0]
        if not self.position:
            if price > ma_val:
                size = int(self.broker.getcash() / price * 0.95)
                self.buy(size=size)
                self.log(f"站上{self.params.ma_period}日线买入 {price:.2f}")
        else:
            if price < ma_val:
                self.close()
                self.log(f"跌破{self.params.ma_period}日线卖出 {price:.2f}")


class BOLLStrategy(bt.Strategy):
    """BOLL(26日)：下轨买入，上轨卖出"""

    params = (("period", 26), ("std", 2), ("printlog", False))

    def __init__(self):
        self.boll = bt.indicators.BollingerBands(
            self.data.close, period=self.params.period, devfactor=self.params.std
        )
        self.order = None

    def log(self, txt):
        if self.params.printlog:
            dt = self.datas[0].datetime.date(0)
            print(f"{dt} {txt}")

    def notify_order(self, order):
        if order.status in [order.Completed, order.Canceled, order.Margin]:
            self.order = None

    def next(self):
        if self.order:
            return
        price = self.data.close[0]
        top = self.boll.lines.top[0]
        bot = self.boll.lines.bot[0]
        self.boll.lines.mid[0]
        if not self.position:
            if price <= bot:
                size = int(self.broker.getcash() / price * 0.95)
                self.buy(size=size)
                self.log(f"BOLL下轨买入 {price:.2f}")
        else:
            if price >= top:
                self.close()
                self.log(f"BOLL上轨卖出 {price:.2f}")


class DMIStrategy(bt.Strategy):
    """DMI趋势：PDI上穿MDI买入，下穿卖出"""

    params = (("period", 14), ("printlog", False))

    def __init__(self):
        self.dmi = bt.indicators.DirectionalMovement(self.data, period=self.params.period)
        self.cross = bt.indicators.CrossOver(self.dmi.lines.plusDI, self.dmi.lines.minusDI)
        self.order = None

    def log(self, txt):
        if self.params.printlog:
            dt = self.datas[0].datetime.date(0)
            print(f"{dt} {txt}")

    def notify_order(self, order):
        if order.status in [order.Completed, order.Canceled, order.Margin]:
            self.order = None

    def next(self):
        if self.order:
            return
        if len(self.data) < 5:
            return
        pdi = self.dmi.lines.plusDI[0]
        mdi = self.dmi.lines.minusDI[0]
        adx = self.dmi.lines.adx[0]
        if not self.position:
            if self.cross[0] == 1 and pdi > mdi:
                size = int(self.broker.getcash() / self.data.close[0] * 0.95)
                self.buy(size=size)
                self.log(f"DMI买入 PDI上穿MDI ADX={adx:.1f}")
        else:
            if self.cross[0] == -1:
                self.close()
                self.log(f"DMI卖出 MDI上穿PDI {self.data.close[0]:.2f}")


class PSYStrategy(bt.Strategy):
    """PSY心理线：低于25超卖买入，高于75超买卖出"""

    params = (("period", 12), ("oversold", 25), ("overbought", 75), ("printlog", False))

    def __init__(self):
        self.order = None

    def log(self, txt):
        if self.params.printlog:
            dt = self.datas[0].datetime.date(0)
            print(f"{dt} {txt}")

    def notify_order(self, order):
        if order.status in [order.Completed, order.Canceled, order.Margin]:
            self.order = None

    def next(self):
        if self.order:
            return
        period = self.params.period
        if len(self.data) < period + 1:
            return
        up_days = 0
        for i in range(-period, 0):
            if self.data.close[i] > self.data.close[i - 1]:
                up_days += 1
        psy = up_days / period * 100
        if not self.position:
            if psy <= self.params.oversold:
                size = int(self.broker.getcash() / self.data.close[0] * 0.95)
                self.buy(size=size)
                self.log(f"PSY超卖买入 {psy:.1f}")
        else:
            if psy >= self.params.overbought:
                self.close()
                self.log(f"PSY超买卖出 {psy:.1f}")


class BIASStrategy(bt.Strategy):
    """5日均线乖离率 ±3% 超跌超涨"""

    params = (("period", 5), ("bias_buy", -3), ("bias_sell", 3), ("printlog", False))

    def __init__(self):
        self.ma = bt.indicators.SMA(self.data.close, period=self.params.period)
        self.order = None

    def log(self, txt):
        if self.params.printlog:
            dt = self.datas[0].datetime.date(0)
            print(f"{dt} {txt}")

    def notify_order(self, order):
        if order.status in [order.Completed, order.Canceled, order.Margin]:
            self.order = None

    def next(self):
        if self.order:
            return
        price = self.data.close[0]
        ma_val = self.ma[0]
        if ma_val == 0:
            return
        bias = (price - ma_val) / ma_val * 100
        if not self.position:
            if bias <= self.params.bias_buy:
                size = int(self.broker.getcash() / price * 0.95)
                self.buy(size=size)
                self.log(f"BIAS超跌买入 {bias:.1f}%")
        else:
            if bias >= self.params.bias_sell:
                self.close()
                self.log(f"BIAS超涨卖出 {bias:.1f}%")


class SARStrategy(bt.Strategy):
    """SAR止损：翻红买入，翻绿卖出"""

    params = (("printlog", False),)

    def __init__(self):
        self.sar = bt.indicators.ParabolicSAR(self.data, af=0.02, afmax=0.2)
        self.order = None

    def log(self, txt):
        if self.params.printlog:
            dt = self.datas[0].datetime.date(0)
            print(f"{dt} {txt}")

    def notify_order(self, order):
        if order.status in [order.Completed, order.Canceled, order.Margin]:
            self.order = None

    def next(self):
        if self.order:
            return
        price = self.data.close[0]
        sar_val = self.sar[0]
        if not self.position:
            if price > sar_val:
                size = int(self.broker.getcash() / price * 0.95)
                self.buy(size=size)
                self.log(f"SAR翻红买入 {price:.2f}")
        else:
            if price < sar_val:
                self.close()
                self.log(f"SAR翻绿卖出 {price:.2f}")


class MACDKDJBOLLStrategy(bt.Strategy):
    """三指标共振：MACD金叉+KDJ金叉+BOLL中轨+站上5日线"""

    params = (("printlog", False),)

    def __init__(self):
        self.macd = bt.indicators.MACD(self.data.close)
        self.macd_cross = bt.indicators.CrossOver(self.macd.macd, self.macd.signal)
        self.k = bt.indicators.Stochastic(self.data)
        self.k_cross = bt.indicators.CrossOver(self.k.lines.percK, self.k.lines.percD)
        self.boll = bt.indicators.BollingerBands(self.data.close, period=26)
        self.ma5 = bt.indicators.SMA(self.data.close, period=5)
        self.order = None

    def log(self, txt):
        if self.params.printlog:
            dt = self.datas[0].datetime.date(0)
            print(f"{dt} {txt}")

    def notify_order(self, order):
        if order.status in [order.Completed, order.Canceled, order.Margin]:
            self.order = None

    def next(self):
        if self.order:
            return
        price = self.data.close[0]
        k_val = self.k.lines.percK[0]
        if not self.position:
            if (
                self.macd_cross[0] == 1
                and self.k_cross[0] == 1
                and price > self.boll.lines.mid[0]
                and price > self.ma5[0]
            ):
                size = int(self.broker.getcash() / price * 0.95)
                self.buy(size=size)
                self.log(f"三指标共振买入 K={k_val:.1f} {price:.2f}")
        else:
            if self.macd_cross[0] == -1 or k_val > 80 or price < self.boll.lines.mid[0]:
                self.close()
                self.log(f"三指标共振卖出 {price:.2f}")


class MACombinationStrategy(bt.Strategy):
    """均线组合(5/10/60)：多头排列买入，走坏卖出"""

    params = (("printlog", False),)

    def __init__(self):
        self.ma5 = bt.indicators.SMA(self.data.close, period=5)
        self.ma10 = bt.indicators.SMA(self.data.close, period=10)
        self.ma60 = bt.indicators.SMA(self.data.close, period=60)
        self.order = None

    def log(self, txt):
        if self.params.printlog:
            dt = self.datas[0].datetime.date(0)
            print(f"{dt} {txt}")

    def notify_order(self, order):
        if order.status in [order.Completed, order.Canceled, order.Margin]:
            self.order = None

    def next(self):
        if self.order:
            return
        price = self.data.close[0]
        m5 = self.ma5[0]
        m10 = self.ma10[0]
        m60 = self.ma60[0]
        if not self.position:
            if price > m5 > m10 > m60:
                size = int(self.broker.getcash() / price * 0.95)
                self.buy(size=size)
                self.log(f"均线多头排列买入 {price:.2f}")
        else:
            if price < m5 or price < m10:
                self.close()
                self.log(f"均线走坏卖出 {price:.2f}")


class VolumePriceDivergenceStrategy(bt.Strategy):
    """量价背离：价创新高量萎缩买入，放量下跌卖出"""

    params = (("lookback", 10), ("printlog", False))

    def __init__(self):
        self.order = None

    def log(self, txt):
        if self.params.printlog:
            dt = self.datas[0].datetime.date(0)
            print(f"{dt} {txt}")

    def notify_order(self, order):
        if order.status in [order.Completed, order.Canceled, order.Margin]:
            self.order = None

    def next(self):
        if self.order:
            return
        if len(self.data) < self.params.lookback + 5:
            return
        price = self.data.close[0]
        volume = self.data.volume[0]
        lookback = self.params.lookback
        recent_high = max(self.data.close.get(size=lookback))
        recent_vol = [self.data.volume[-i] for i in range(1, lookback + 1)]
        avg_vol = sum(recent_vol) / len(recent_vol)
        if not self.position:
            if price >= recent_high and volume < avg_vol * 0.7:
                self.log(f"量价背离买入 {price:.2f}")
                size = int(self.broker.getcash() / price * 0.95)
                self.buy(size=size)
        else:
            if volume > avg_vol * 1.5 and price < self.data.close[-1]:
                self.close()
                self.log(f"放量下跌卖出 {price:.2f}")


class DMIAndPSYStrategy(bt.Strategy):
    """DMI+PSY超跌反弹：PDI<5 + PSY≤25"""

    params = (("printlog", False),)

    def __init__(self):
        self.dmi = bt.indicators.DirectionalMovement(self.data, period=14)
        self.order = None

    def log(self, txt):
        if self.params.printlog:
            dt = self.datas[0].datetime.date(0)
            print(f"{dt} {txt}")

    def notify_order(self, order):
        if order.status in [order.Completed, order.Canceled, order.Margin]:
            self.order = None

    def next(self):
        if self.order:
            return
        if len(self.data) < 13:
            return
        plusDI = self.dmi.lines.plusDI[0]
        up_days = 0
        for i in range(-12, 0):
            if self.data.close[i] > self.data.close[i - 1]:
                up_days += 1
        psy = up_days / 12 * 100
        if not self.position:
            if plusDI < 5 and psy <= 25:
                size = int(self.broker.getcash() / self.data.close[0] * 0.95)
                self.buy(size=size)
                self.log(f"DMI+PSY超跌买入 PDI={plusDI:.1f} PSY={psy:.1f}")
        else:
            if self.dmi.lines.adx[0] > 0 and self.dmi.lines.adx[0] < self.dmi.lines.adx[-1]:
                self.close()
                self.log(f"ADX转头卖出 {self.data.close[0]:.2f}")


class ThreeThirdStrategy(bt.Strategy):
    """三分法(7/13/20)：分批买入卖出"""

    params = (("printlog", False),)

    def __init__(self):
        self.ma7 = bt.indicators.SMA(self.data.close, period=7)
        self.ma13 = bt.indicators.SMA(self.data.close, period=13)
        self.ma20 = bt.indicators.SMA(self.data.close, period=20)
        self.order = None

    def log(self, txt):
        if self.params.printlog:
            dt = self.datas[0].datetime.date(0)
            print(f"{dt} {txt}")

    def notify_order(self, order):
        if order.status in [order.Completed, order.Canceled, order.Margin]:
            self.order = None

    def next(self):
        if self.order:
            return
        price = self.data.close[0]
        m7 = self.ma7[0]
        m13 = self.ma13[0]
        m20 = self.ma20[0]
        cash = self.broker.getcash()
        value = self.broker.getvalue()
        pos_pct = (value - cash) / value * 100
        if not self.position:
            cash_per_trade = cash * 0.3
            if price > m7 and pos_pct < 25:
                size = int(cash_per_trade / price * 0.95)
                if size > 0:
                    self.buy(size=size)
                    self.log(f"三分法站上7日线买入 {price:.2f}")
            elif price > m13 and pos_pct < 50:
                cash_new = self.broker.getcash()
                size = int(cash_new * 0.3 / price * 0.95)
                if size > 0:
                    self.buy(size=size)
                    self.log(f"三分法站上13日线买入 {price:.2f}")
            elif price > m20 and pos_pct < 75:
                cash_new = self.broker.getcash()
                size = int(cash_new * 0.3 / price * 0.95)
                if size > 0:
                    self.buy(size=size)
                    self.log(f"三分法站上20日线满仓 {price:.2f}")
        else:
            if price < m7 and pos_pct > 0:
                self.sell(size=int(self.position.size * 0.34))
                self.log(f"三分法跌破7日线出1/3 {price:.2f}")
            if price < m13 and pos_pct > 0:
                self.sell(size=int(self.position.size * 0.5))
                self.log(f"三分法跌破13日线再出1/3 {price:.2f}")
            if price < m20:
                self.close()
                self.log(f"三分法跌破20日线清仓 {price:.2f}")


class SparrowStrategy(bt.Strategy):
    """麻雀战术：每次赚2.5%止盈，跌破买入价止损"""

    params = (("profit_target", 2.5), ("printlog", False))

    def __init__(self):
        self.order = None
        self.entry_price = 0.0

    def log(self, txt):
        if self.params.printlog:
            dt = self.datas[0].datetime.date(0)
            print(f"{dt} {txt}")

    def notify_order(self, order):
        if order.status in [order.Completed, order.Canceled, order.Margin]:
            self.order = None
            if order.status == order.Completed and order.isbuy():
                self.entry_price = order.executed.price

    def next(self):
        if self.order:
            return
        price = self.data.close[0]
        if not self.position:
            self.buy(size=int(self.broker.getcash() / price * 0.95))
            self.log(f"麻雀战术买入 {price:.2f}")
        else:
            if self.entry_price > 0:
                pnl_pct = (price - self.entry_price) / self.entry_price * 100
                if pnl_pct >= self.params.profit_target:
                    self.close()
                    self.log(f"麻雀止盈 {price:.2f} +{pnl_pct:.1f}%")
                elif pnl_pct <= -0.5:
                    self.close()
                    self.log(f"麻雀止损 {price:.2f} {pnl_pct:.1f}%")


class BounceStrategy(bt.Strategy):
    """反弹量化：涨幅>昨日跌幅50% + 成交量>昨日20%"""

    params = (("printlog", False),)

    def __init__(self):
        self.order = None

    def log(self, txt):
        if self.params.printlog:
            dt = self.datas[0].datetime.date(0)
            print(f"{dt} {txt}")

    def notify_order(self, order):
        if order.status in [order.Completed, order.Canceled, order.Margin]:
            self.order = None

    def next(self):
        if self.order:
            return
        if len(self.data) < 3:
            return
        price = self.data.close[0]
        volume = self.data.volume[0]
        prev_close = self.data.close[-1]
        prev_volume = self.data.volume[-1]
        if prev_close == 0 or prev_volume == 0:
            return
        yesterday_change = (prev_close - self.data.close[-2]) / self.data.close[-2] * 100
        if yesterday_change >= 0:
            return
        today_change = (price - prev_close) / prev_close * 100
        vol_change = (volume - prev_volume) / prev_volume * 100
        if not self.position:
            if today_change > abs(yesterday_change) * 0.5 and vol_change > 20:
                size = int(self.broker.getcash() / price * 0.2)
                if size > 0:
                    self.buy(size=size)
                    self.log(f"反弹买入 涨幅{today_change:.1f}% 量增{vol_change:.1f}%")
        else:
            if today_change < -5:
                self.close()
                self.log(f"反弹止损 {price:.2f}")


class TwoLineStrategy(bt.Strategy):
    """二线法：5日线下穿10日线清仓，上穿短线操作"""

    params = (("printlog", False),)

    def __init__(self):
        self.ma5 = bt.indicators.SMA(self.data.close, period=5)
        self.ma10 = bt.indicators.SMA(self.data.close, period=10)
        self.cross = bt.indicators.CrossOver(self.ma5, self.ma10)
        self.order = None

    def log(self, txt):
        if self.params.printlog:
            dt = self.datas[0].datetime.date(0)
            print(f"{dt} {txt}")

    def notify_order(self, order):
        if order.status in [order.Completed, order.Canceled, order.Margin]:
            self.order = None

    def next(self):
        if self.order:
            return
        if not self.position:
            if self.cross[0] == 1:
                size = int(self.broker.getcash() / self.data.close[0] * 0.95)
                self.buy(size=size)
                self.log(f"二线法5日上穿10日买入 {self.data.close[0]:.2f}")
        else:
            if self.cross[0] == -1:
                self.close()
                self.log(f"二线法5日下穿10日清仓 {self.data.close[0]:.2f}")


class LifeLine60Strategy(bt.Strategy):
    """60日生命线：站上60日线做多，跌破空仓"""

    params = (("printlog", False),)

    def __init__(self):
        self.ma60 = bt.indicators.SMA(self.data.close, period=60)
        self.order = None

    def log(self, txt):
        if self.params.printlog:
            dt = self.datas[0].datetime.date(0)
            print(f"{dt} {txt}")

    def notify_order(self, order):
        if order.status in [order.Completed, order.Canceled, order.Margin]:
            self.order = None

    def next(self):
        if self.order:
            return
        price = self.data.close[0]
        m60 = self.ma60[0]
        if not self.position:
            if price > m60:
                size = int(self.broker.getcash() / price * 0.95)
                self.buy(size=size)
                self.log(f"60日生命线站上买入 {price:.2f}")
        else:
            if price < m60:
                self.close()
                self.log(f"60日生命线跌破卖出 {price:.2f}")
