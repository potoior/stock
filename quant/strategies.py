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
    params = (("period", 20), ("std", 2), ("printlog", False))
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
        mid = self.boll.lines.mid[0]
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
        self.cross = bt.indicators.CrossOver(self.dmi.lines.pdi, self.dmi.lines.mdi)
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
    params = (("period", 5), ("bias_buy", -5), ("bias_sell", 5), ("printlog", False))
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
    """三指标共振：MACD金叉 + KDJ超卖 + BOLL下轨 同时满足买入"""
    params = (("printlog", False),)
    def __init__(self):
        self.macd = bt.indicators.MACD(self.data.close)
        self.macd_cross = bt.indicators.CrossOver(self.macd.macd, self.macd.signal)
        self.k = bt.indicators.Stochastic(self.data)
        self.boll = bt.indicators.BollingerBands(self.data.close)
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
        boll_bot = self.boll.lines.bot[0]
        boll_mid = self.boll.lines.mid[0]
        if not self.position:
            buy = 0
            if self.macd_cross[0] == 1: buy += 1
            if k_val < 30: buy += 1
            if price <= boll_bot * 1.01: buy += 1
            if self.macd.macd[0] > self.macd.signal[0]: buy += 1
            if buy >= 2:
                size = int(self.broker.getcash() / price * 0.95)
                self.buy(size=size)
                self.log(f"共振买入得分{buy} {price:.2f}")
        else:
            if self.macd_cross[0] == -1 or k_val > 80 or price >= self.boll.lines.top[0]:
                self.close()
                self.log(f"共振卖出 {price:.2f}")

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
        prev_avg_vol = sum(recent_vol[:lookback//2]) / (lookback // 2)
        if not self.position:
            if price >= recent_high and volume < avg_vol * 0.7:
                self.log(f"量价背离买入 {price:.2f}")
                size = int(self.broker.getcash() / price * 0.95)
                self.buy(size=size)
        else:
            if volume > avg_vol * 1.5 and price < self.data.close[-1]:
                self.close()
                self.log(f"放量下跌卖出 {price:.2f}")