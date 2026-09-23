"""Parser tests built from real messages posted in the monitored channels."""
import pytest

from tgtrader.parser import LONG, SHORT, Signal, Update, parse_message


def sig(text) -> Signal:
    r = parse_message(text)
    assert isinstance(r, Signal), r
    return r


def test_ivan_btc_long_with_limit_range():
    s = sig("BTC（100X做多📈）\n\n進場：限價81900—80999\n\n止盈：84438—86076—90171\n\n離場：79443")
    assert (s.symbol, s.side, s.leverage) == ("BTC", LONG, 100)
    assert (s.entry_low, s.entry_high) == (80999, 81900)
    assert s.take_profits == [84438, 86076, 90171]
    assert s.stop_loss == 79443
    assert not s.market


def test_ivan_coti_market_no_stop():
    s = sig("$COTI (50X做多📈)\n\n進場：市價0.0155附近—0.01518\n\n🚀🚀兄弟們點贊衝上月球")
    assert (s.symbol, s.side, s.leverage, s.market) == ("COTI", LONG, 50, True)
    assert (s.entry_low, s.entry_high) == (0.01518, 0.0155)
    assert s.stop_loss is None


def test_caicai_tia_short():
    s = sig("#TIA 輕倉市價空\n\n進場 : 0.4436\n\n✅止盈：0.4311-0.4134\n\n❌止損：0.4601")
    assert (s.symbol, s.side, s.market) == ("TIA", SHORT, True)
    assert s.entry_low == s.entry_high == 0.4436
    assert s.take_profits == [0.4311, 0.4134]
    assert s.stop_loss == 0.4601


def test_caicai_single_line_repost():
    s = sig("#TIA 輕倉市價空  進場 :0.5073  ✅止盈：0.4930-0.4728  ❌止損：0.5262")
    assert s.entry_low == 0.5073
    assert s.take_profits == [0.4930, 0.4728]
    assert s.stop_loss == 0.5262


def test_caicai_bare_entry_line():
    s = sig("#HYPE   輕倉市價多\n96.4-96.8\n✅止盈：99.8-104-113\n❌止損：93.7")
    assert (s.symbol, s.side) == ("HYPE", LONG)
    assert (s.entry_low, s.entry_high) == (96.4, 96.8)
    assert s.take_profits == [99.8, 104, 113]
    assert s.stop_loss == 93.7


def test_caicai_bare_entry_single_line():
    s = sig("#HYPE   輕倉市價多 96.4-96.8 ✅止盈：99.8-104-113 ❌止損：93.7")
    assert (s.entry_low, s.entry_high) == (96.4, 96.8)
    assert s.take_profits == [99.8, 104, 113]
    assert s.stop_loss == 93.7


def test_chun_short_without_entry():
    s = sig("#WIF  輕倉市價空\n\n止盈：0.2441\n止損：0.2649")
    assert (s.symbol, s.side, s.market) == ("WIF", SHORT, True)
    assert s.entry_low is None
    assert s.take_profits == [0.2441]
    assert s.stop_loss == 0.2649


def test_chun_short_short_single_line():
    s = sig("#ENA   輕倉市價短空  止盈：0.2402 止損：0.2608")
    assert (s.symbol, s.side) == ("ENA", SHORT)
    assert s.take_profits == [0.2402]
    assert s.stop_loss == 0.2608


def test_yuyanjia_eth_short():
    s = sig("🖥 交易信號 🖥\n\n#ETH （100x做空👇👇👇）\n\n✏️進場位： 2778\n\n👁 盈利位：2700—2600\n\n ❌止損位：2830")
    assert (s.symbol, s.side, s.leverage) == ("ETH", SHORT, 100)
    assert s.entry_low == 2778
    assert s.take_profits == [2700, 2600]
    assert s.stop_loss == 2830


def test_yuyanjia_market_long_no_levels():
    s = sig("#DYM 市價輕倉多")
    assert (s.symbol, s.side, s.market) == ("DYM", LONG, True)
    assert s.stop_loss is None and s.take_profits == []


@pytest.mark.parametrize("text", [
    "#HYPE 今天想做空這個😀",
    "#DYM 翻倍\n\n突破看0.02",
    "#ETH #HYPE 空單雙雙翻倍🐤",
    "tp1止盈，夜晚注意倉位管理",
    "翻倍了，摸了半天了！",
    "#你所錯過的是我正在盈利的  繼續千趴結算👍  入群私訊 @zhuli_wahahaha 🐤",
    "重大行情機會來襲🔥\n\n想了解詳情，直接諮詢助理 @Stella0_q\n添加時備註專屬暗號：Caicai123",
    "🪙7-9月帶單2️⃣5️⃣1️⃣2️⃣8️⃣U落袋  📱目前主打20—50U保證金，利潤3-5倍就落袋",
])
def test_noise_is_ignored(text):
    assert not isinstance(parse_message(text), Signal)


def test_move_stop_update():
    u = parse_message("$COTI\n\n第二止盈看0.01742\n\n提損0.01553，這單有潛力10倍")
    assert isinstance(u, Update)
    assert (u.symbol, u.action, u.price) == ("COTI", "move_sl", 0.01553)


def test_reduce_update():
    u = parse_message("$COTI\n\n兄弟們💪💪\n\n減倉30%，等0.01618再套保")
    assert isinstance(u, Update)
    assert (u.action, u.percent) == ("reduce", 30)


def test_stop_on_wrong_side_is_rejected():
    assert parse_message("#BTC 做多 進場:100 止損:105 止盈:110") is None


def test_english_signal():
    s = sig("#SOLUSDT LONG 10x\nEntry: 150-152\nTP: 158 / 165\nSL: 145")
    assert (s.symbol, s.side, s.leverage) == ("SOL", LONG, 10)
    assert (s.entry_low, s.entry_high) == (150, 152)
    assert s.take_profits == [158, 165]
    assert s.stop_loss == 145
