import pytest

from tgtrader.config import ChannelCfg, LlmCfg, RiskCfg, Settings
from tgtrader.llm_parser import to_parsed
from tgtrader.parser import LONG, SHORT, Signal, Update, parse_message
from tgtrader.risk import Rejected, build_plan, split_amounts
from tgtrader.store import Store
from tgtrader.trader import Trader


# ----------------------------------------------------------------------------- risk
def test_leverage_is_capped_and_reduced_for_wide_stops():
    s = Signal("BTC", LONG, entry_low=100, entry_high=100, stop_loss=90, take_profits=[120], leverage=100)
    p = build_plan(s, 100, RiskCfg(max_leverage=10, max_loss_at_stop_pct=60))
    # 10% stop -> at most 6x so the stop hits before liquidation
    assert p.leverage == 6
    assert p.notional == pytest.approx(10 * 6)


def test_requires_stop_loss():
    s = Signal("DYM", LONG, market=True)
    with pytest.raises(Rejected):
        build_plan(s, 1.0, RiskCfg(require_stop_loss=True))
    p = build_plan(s, 1.0, RiskCfg(require_stop_loss=False, default_stop_loss_pct=3, default_tp_rr=2))
    assert p.stop_loss == pytest.approx(0.97)
    assert p.take_profits == [pytest.approx(1.06)]


def test_chasing_uses_limit_or_rejects():
    s = Signal("ETH", SHORT, entry_low=2778, entry_high=2778, stop_loss=2830, take_profits=[2700])
    p = build_plan(s, 2700, RiskCfg(use_limit_orders=True))  # price already fell 2.8%
    assert p.order_type == "limit" and p.entry_price == 2778
    with pytest.raises(Rejected):
        build_plan(s, 2700, RiskCfg(use_limit_orders=False))
    assert build_plan(s, 2775, RiskCfg()).order_type == "market"


def test_price_mismatch_rejected():
    s = Signal("PEPE", LONG, entry_low=0.00001, entry_high=0.00001, stop_loss=0.000009)
    with pytest.raises(Rejected):
        build_plan(s, 0.01, RiskCfg())


def test_fixed_risk_sizing_and_cap():
    s = Signal("SOL", LONG, entry_low=100, entry_high=100, stop_loss=98, take_profits=[104])
    p = build_plan(s, 100, RiskCfg(sizing="fixed_risk", risk_usdt=3, max_notional_usdt=1000))
    assert p.notional == pytest.approx(150)
    assert p.loss_at_stop == pytest.approx(3)
    p = build_plan(s, 100, RiskCfg(sizing="fixed_risk", risk_usdt=3, max_notional_usdt=100))
    assert p.notional == 100


def test_channel_override():
    s = Signal("SOL", LONG, entry_low=100, entry_high=100, stop_loss=99, take_profits=[104], leverage=20)
    p = build_plan(s, 100, RiskCfg(margin_usdt=10, max_leverage=10), ChannelCfg("x", margin_usdt=2, max_leverage=3))
    assert (p.leverage, p.notional) == (3, 6)


def test_split_amounts():
    assert split_amounts(10, 3, [0.5, 0.3, 0.2]) == pytest.approx([5, 3, 2])
    assert split_amounts(10, 2, [0.5, 0.3, 0.2]) == pytest.approx([6.25, 3.75])
    assert split_amounts(10, 4, [0.5, 0.5]) == pytest.approx([2.5, 2.5, 2.5, 2.5])


def test_llm_output_mapping():
    d = {"kind": "signal", "symbol": "btcusdt", "side": "long", "market_order": False, "entry_low": 101,
         "entry_high": 100, "take_profits": [110, 90], "stop_loss": 95, "leverage": 20,
         "update_action": None, "update_price": None, "update_percent": None}
    s = to_parsed(d, "raw")
    assert isinstance(s, Signal) and s.symbol == "BTC" and (s.entry_low, s.entry_high) == (100, 101)
    assert s.take_profits == [110]
    u = to_parsed({**d, "kind": "update", "update_action": "reduce", "update_percent": 30}, "raw")
    assert isinstance(u, Update) and u.percent == 30


# --------------------------------------------------------------------------- trader
class FakeExchange:
    def __init__(self, prices):
        self.prices = dict(prices)
        self.markets = {
            f"{b}/USDT:USDT": {"symbol": f"{b}/USDT:USDT", "swap": True, "active": True, "contractSize": 1,
                               "limits": {"amount": {"min": 0.001}, "cost": {"min": 5}}}
            for b in ("BTC", "ETH", "TIA", "HYPE")
        }
        self.markets["1000PEPE/USDT:USDT"] = {**self.markets["BTC/USDT:USDT"], "symbol": "1000PEPE/USDT:USDT"}
        self.orders = []
        self.cancelled = []
        self.position = 0.0

    def market(self, s):
        return self.markets[s]

    def amount_to_precision(self, s, a):
        return f"{int(a * 1000) / 1000:.3f}"

    def price_to_precision(self, s, p):
        return f"{p:.6g}"

    async def fetch_ticker(self, s):
        return {"last": self.prices[s]}

    async def fetch_positions(self, symbols=None):
        if not self.position:
            return []
        return [{"symbol": o["symbol"], "contracts": self.position} for o in self.orders[:1]]

    async def create_order(self, symbol, type, side, amount, price=None, params=None):
        o = {"id": str(len(self.orders) + 1), "symbol": symbol, "type": type, "side": side, "amount": amount,
             "price": price, "params": params or {}, "filled": amount, "average": self.prices[symbol]}
        self.orders.append(o)
        if not (params or {}).get("reduceOnly") and type == "market":
            self.position += amount
        return o

    async def cancel_order(self, oid, symbol, params=None):
        self.cancelled.append(oid)

    async def cancel_all_orders(self, symbol, params=None):
        self.cancelled.append(f"all:{symbol}")

    async def set_margin_mode(self, *a, **k):
        pass

    async def set_leverage(self, *a, **k):
        pass

    async def fetch_balance(self):
        return {"USDT": {"total": 1000}}

    async def close(self):
        pass


def make_trader(tmp_path, dry=True, prices=None, **risk):
    s = Settings(dry_run=dry, api_id=1, api_hash="x", phone="", session_path="x", exchange_id="binance",
                 exchange_key="" if dry else "k", exchange_secret="", exchange_password="", testnet=False,
                 margin_mode="isolated", channels=[ChannelCfg("chan")], risk=RiskCfg(**risk), llm=LlmCfg(),
                 db_path=str(tmp_path / "t.db"))
    notes = []

    async def notify(msg):
        notes.append(msg)

    t = Trader(s, Store(s.db_path), notify)
    t.ex = FakeExchange(prices or {"TIA/USDT:USDT": 0.44, "BTC/USDT:USDT": 81500, "1000PEPE/USDT:USDT": 0.01})
    return t, notes


async def test_dry_run_full_cycle(tmp_path):
    t, notes = make_trader(tmp_path)
    sig = parse_message("#TIA 輕倉市價空\n進場 : 0.4436\n✅止盈：0.4311-0.4134\n❌止損：0.4601")
    msg = await t.open_signal(sig, "chan")
    assert "模拟" in msg and t.ex.orders == []
    [trade] = t.store.active_trades()
    assert trade.side == SHORT and trade.stop_loss == 0.4601

    with pytest.raises(Rejected):          # same coin twice
        await t.open_signal(sig, "chan")

    t.ex.prices["TIA/USDT:USDT"] = 0.43    # TP1 hit -> SL to entry
    await t.sync()
    [trade] = t.store.active_trades()
    assert trade.tp_hits == 1 and trade.stop_loss == trade.entry_price and trade.pnl > 0

    t.ex.prices["TIA/USDT:USDT"] = 0.45    # back to break-even stop
    await t.sync()
    assert t.store.active_trades() == []
    assert t.store.channel_stats()[0]["wins"] == 1


async def test_dry_run_1000_prefix_scaling(tmp_path):
    t, _ = make_trader(tmp_path)
    sig = Signal("PEPE", LONG, entry_low=0.00001, entry_high=0.00001, stop_loss=0.0000097, take_profits=[0.000011])
    await t.open_signal(sig, "chan")
    [trade] = t.store.active_trades()
    assert trade.symbol == "1000PEPE/USDT:USDT"
    assert trade.stop_loss == pytest.approx(0.0097)


async def test_live_market_entry_places_sl_and_tps(tmp_path):
    t, notes = make_trader(tmp_path, dry=False, margin_usdt=20, max_leverage=10)
    sig = parse_message("BTC（100X做多📈）\n進場：限價81900—80999\n止盈：84438—86076—90171\n離場：79443")
    msg = await t.open_signal(sig, "chan")
    assert msg.startswith("✅")
    entry, sl, *tps = t.ex.orders
    assert (entry["type"], entry["side"]) == ("market", "buy")
    assert entry["amount"] == pytest.approx(0.002)            # 20U * 10x / 81500
    assert sl["params"] == {"stopLossPrice": 79443, "reduceOnly": True} and sl["side"] == "sell"
    # 0.002 BTC split 50/30/20 -> below the 0.001 lot, so the last level folds into the second
    assert [o["params"]["takeProfitPrice"] for o in tps] == [84438, 86076]
    assert [o["amount"] for o in tps] == [0.001, 0.001]
    [trade] = t.store.active_trades()
    assert trade.sl_order == sl["id"]

    # Channel follow-up moves the stop
    msg = await t.apply_update(Update("BTC", "move_sl", price=80500), "chan")
    assert "80500" in msg and t.ex.orders[-1]["params"]["stopLossPrice"] == 80500
    with pytest.raises(Rejected):          # other channels can't touch it
        await t.apply_update(Update("BTC", "close"), "other")

    # Position closed on the exchange -> leftover orders cancelled
    t.ex.position = 0
    await t.sync()
    assert t.store.active_trades() == [] and "all:BTC/USDT:USDT" in t.ex.cancelled


async def test_live_sl_failure_closes_position(tmp_path):
    t, notes = make_trader(tmp_path, dry=False, margin_usdt=20)
    orig = t.ex.create_order

    async def flaky(symbol, type, side, amount, price=None, params=None):
        if params and "stopLossPrice" in params:
            raise RuntimeError("boom")
        return await orig(symbol, type, side, amount, price, params)

    t.ex.create_order = flaky
    sig = parse_message("#TIA 輕倉市價空\n進場 : 0.4436\n✅止盈：0.4311-0.4134\n❌止損：0.4601")
    await t.open_signal(sig, "chan")
    assert t.ex.orders[-1]["params"] == {"reduceOnly": True}   # emergency close
    assert any("止损下单失败" in n for n in notes)
    assert t.store.active_trades() == []
