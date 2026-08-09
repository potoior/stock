import backtrader as bt

class MACDStrategy(bt.Strategy):
    params = (
        ("fast", 12), ("slow", 26), ("signal", 9),
        ("printlog", False),
    )

    def __init__(self):
        self.macd = bt.indicators.MACD(
            self.data.close,
            period_me1=self.params.fast,
            period_me2=self.params.slow,
            period_signal=self.params.signal,
        )
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
        if self.order:
            return
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
    params = (
        ("period", 9), ("fast", 3), ("slow", 3),
        ("overbought", 80), ("oversold", 20),
        ("printlog", False),
    )

    def __init__(self):
        self.k = bt.indicators.Stochastic(
            self.data,
            period=self.params.period,
            period_dfast=self.params.fast,
            period_dslow=self.params.slow,
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
    params = (
        ("ma_period", 5),
        ("printlog", False),
    )

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