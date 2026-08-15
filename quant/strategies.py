import backtrader as bt

class MACDStrategy(bt.Strategy):
    params = (("fast", 12), ("slow", 26), ("signal", 9), ("printlog", False))
    def __init__(self):
        self.macd = bt.indicators.MACD(self.data.close, period_me1=self.params.fast, period_me2=self.params.slow, period_signal=self.params.signal)
        self.cross = bt.indicators.CrossOver(self.macd.macd, self.macd.signal)
        self.order = None
    def log(self, txt):
        if self.params.printlog:
            dt = self.datas[0].datetime.date(0)
            print(f"{dt} {txt}")
    def notify_order(self, order):
        if order.status in [order.Completed, order.Canceled, order.Margin]:
            self.order = None
    def next(self):
        if self.order: return
        if not self.position:
            if self.cross[0] == 1:
                size = int(self.broker.getcash() / self.data.close[0] * 0.95)
                self.buy(size=size)
                self.log(f"MACD金叉买入 {self.data.close[0]:.2f}")
        else:
            if self.cross[0] == -1:
                self.close()
                self.log(f"MACD死叉卖出 {self.data.close[0]:.2f}")

class KDJStrategy(bt.Strategy):
    params = (("period", 9), ("fast", 3), ("slow", 3), ("overbought", 80), ("oversold", 20), ("printlog", False))
    def __init__(self):
        self.k = bt.indicators.Stochastic(self.data, period=self.params.period, period_dfast=self.params.fast, period_dslow=self.params.slow)
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
        if self.order: return
        k_val = self.k.lines.percK[0]
        if not self.position:
            if self.cross[0] == 1 and k_val < self.params.oversold:
                size = int(self.broker.getcash() / self.data.close[0] * 0.95)
                self.buy(size=size)
                self.log(f"KDJ买入 K={k_val:.1f}")
        else:
            if self.cross[0] == -1 and k_val > self.params.overbought:
                self.close()
                self.log(f"KDJ卖出 K={k_val:.1f}")

class MAStopStrategy(bt.Strategy):
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
        if self.order: return
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
    params = (("period", 26), ("std", 2), ("printlog", False))
    def __init__(self):
        self.boll = bt.indicators.BollingerBands(self.data.close, period=self.params.period, devfactor=self.params.std)
        self.order = None
    def log(self, txt):
        if self.params.printlog:
            dt = self.datas[0].datetime.date(0)
            print(f"{dt} {txt}")
    def notify_order(self, order):
        if order.status in [order.Completed, order.Canceled, order.Margin]:
            self.order = None
    def next(self):
        if self.order: return
        price = self.data.close[0]
        top = self.boll.lines.top[0]
        bot = self.boll.lines.bot[0]
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
        if self.order: return
        if not self.position:
            if self.cross[0] == 1:
                size = int(self.broker.getcash() / self.data.close[0] * 0.95)
                self.buy(size=size)
                self.log(f"DMI买入 PDI上穿MDI {self.data.close[0]:.2f}")
        else:
            if self.cross[0] == -1:
                self.close()
                self.log(f"DMI卖出 MDI上穿PDI {self.data.close[0]:.2f}")

class DMIAndPSYStrategy(bt.Strategy):
    """DMI+PSY 超跌反弹：PDI<5 + PSY≤25 捕捉反弹"""
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
        if self.order: return
        if len(self.data) < 13: return
        plusDI = self.dmi.lines.plusDI[0]
        up_days = 0
        for i in range(-12, 0):
            if self.data.close[i] > self.data.close[i-1]:
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

class PSYStrategy(bt.Strategy):
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
        if self.order: return
        period = self.params.period
        if len(self.data) < period + 1: return
        up_days = 0
        for i in range(-period, 0):
            if self.data.close[i] > self.data.close[i-1]:
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
    """5日均线乖离率，大盘股±2%，小盘股±4%，此处用3%折中"""
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
        if self.order: return
        price = self.data.close[0]
        ma_val = self.ma[0]
        if ma_val == 0: return
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
        if self.order: return
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
    """三指标共振：MACD金叉 + KDJ金叉(低值) + BOLL中轨 + 站上5日线"""
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
        if self.order: return
        price = self.data.close[0]
        k_val = self.k.lines.percK[0]
        d_val = self.k.lines.percD[0]
        if not self.position:
            if (self.macd_cross[0] == 1 and self.k_cross[0] == 1
                and price > self.boll.lines.mid[0]
                and price > self.ma5[0]):
                size = int(self.broker.getcash() / price * 0.95)
                self.buy(size=size)
                self.log(f"三指标共振买入 K={k_val:.1f} D={d_val:.1f} {price:.2f}")
        else:
            if self.macd_cross[0] == -1 or k_val > 80 or price < self.boll.lines.mid[0]:
                self.close()
                self.log(f"三指标共振卖出 {price:.2f}")

class MACombinationStrategy(bt.Strategy):
    """均线系统组合：5日+10日+60日多周期确认"""
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
        if self.order: return
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
    """量价背离：价格创新高但成交量萎缩"""
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
        if self.order: return
        if len(self.data) < self.params.lookback + 5: return
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

class ThreeThirdStrategy(bt.Strategy):
    """三分法：7日/13日/20日线，分三批买入卖出"""
    params = (("printlog", False),)
    def __init__(self):
        self.ma7 = bt.indicators.SMA(self.data.close, period=7)
        self.ma13 = bt.indicators.SMA(self.data.close, period=13)
        self.ma20 = bt.indicators.SMA(self.data.close, period=20)
        self.order = None
        self.entry_price = 0.0
    def log(self, txt):
        if self.params.printlog:
            dt = self.datas[0].datetime.date(0)
            print(f"{dt} {txt}")
    def notify_order(self, order):
        if order.status in [order.Completed, order.Canceled, order.Margin]:
            self.order = None
    def next(self):
        if self.order: return
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
    """麻雀战术：每次赚2.5%就卖，跌破买入价止损"""
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
        if self.order: return
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
        if self.order: return
        if len(self.data) < 3: return
        price = self.data.close[0]
        volume = self.data.volume[0]
        prev_close = self.data.close[-1]
        prev_volume = self.data.volume[-1]
        if prev_close == 0 or prev_volume == 0: return
        yesterday_change = (prev_close - self.data.close[-2]) / self.data.close[-2] * 100
        if yesterday_change >= 0: return
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