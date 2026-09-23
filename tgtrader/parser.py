"""Rule-based parser for Chinese / English crypto futures signals.

Handles the formats seen in the monitored channels, e.g.

    BTC（100X做多📈）
    進場：限價81900—80999
    止盈：84438—86076—90171
    離場：79443

    #TIA 輕倉市價空
    進場 : 0.4436
    ✅止盈：0.4311-0.4134
    ❌止損：0.4601

    #HYPE   輕倉市價多
    96.4-96.8
    ✅止盈：99.8-104-113
    ❌止損：93.7

and follow-up management messages such as "$COTI 提損0.01553" or "減倉30%".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Union

LONG, SHORT = "long", "short"

_NUM = r"\d+(?:\.\d+)?"

_TRANSLATE = str.maketrans({
    "：": ":", "（": "(", "）": ")", "—": "-", "–": "-", "－": "-", "~": "-", "～": "-",
    "，": ",", "、": ",", "／": "/", "＄": "$", "＃": "#", "％": "%", "Ｘ": "X", "ｘ": "x",
})

# Order matters only for readability; any hit counts.
_SHORT_WORDS = [
    "做空", "空單", "空单", "短空", "市價空", "市价空", "輕倉空", "轻仓空", "開空", "开空",
    "空進", "空进", "看空進場", "short", "sell",
]
_LONG_WORDS = [
    "做多", "多單", "多单", "短多", "市價多", "市价多", "輕倉多", "轻仓多", "開多", "开多",
    "多進", "多进", "long", "buy",
]

# Words that mark something as a real trade call (vs. chatter like "今天想做空這個").
_ACTION_RE = re.compile(
    r"市[價价]|限[價价]|進場|进场|入場|入场|開倉|开仓|止損|止损|止盈|離場|离场|盈利位"
    r"|(?<![a-z])(?:entry|stop|tp\d?|sl)(?![a-z])", re.IGNORECASE)

_ENTRY_KEYS = r"(?:進場位?|进场位?|入場|入场|開倉價?|开仓价?|entry|限[價价])"
_TP_KEYS = r"(?:止盈|盈利位|目標位?|目标位?|take\s*profit|(?<![a-z])tp\d?(?![a-z])|targets?)"
_SL_KEYS = r"(?:止損位?|止损位?|離場|离场|stop\s*loss|(?<![a-z])sl(?![a-z])|(?<![a-z])stop(?![a-z]))"

_SYMBOL_BLACKLIST = {"USDT", "USD", "TP", "SL", "VIP", "AI", "U"}


@dataclass
class Signal:
    symbol: str                      # base asset, e.g. "BTC"
    side: str                        # "long" | "short"
    market: bool = False             # signal says 市價 / market
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    take_profits: list[float] = field(default_factory=list)
    stop_loss: Optional[float] = None
    leverage: Optional[int] = None
    raw: str = ""
    source: str = "regex"

    @property
    def entry_ref(self) -> Optional[float]:
        if self.entry_low is None:
            return None
        return (self.entry_low + (self.entry_high or self.entry_low)) / 2


@dataclass
class Update:
    """Management instruction for an existing position."""
    symbol: str
    action: str                      # "move_sl" | "reduce" | "close"
    price: Optional[float] = None    # new stop for move_sl
    percent: Optional[float] = None  # 0-100 for reduce
    raw: str = ""


Parsed = Union[Signal, Update, None]


def normalize(text: str) -> str:
    return text.translate(_TRANSLATE)


def _numbers(s: str) -> list[float]:
    return [float(x) for x in re.findall(_NUM, s)]


def _after_key(text: str, key_re: str) -> Optional[str]:
    """Return the rest of the line after the first occurrence of key_re."""
    m = re.search(key_re + r"\s*:?\s*(.*)", text, re.IGNORECASE)
    return m.group(1) if m else None


def _values_after(text: str, key_re: str) -> list[float]:
    rest = _after_key(text, key_re)
    if rest is None:
        return []
    rest = re.sub(r"^\s*(?:限[價价]|市[價价])\s*", "", rest)
    # Cut at the next keyword on the same line (single-line reposts join everything).
    rest = re.split(_TP_KEYS + "|" + _SL_KEYS + "|" + _ENTRY_KEYS + "|[✅❌🚀✏👁]", rest,
                    maxsplit=1, flags=re.IGNORECASE)[0]
    return _numbers(rest)


def extract_symbol(text: str) -> Optional[str]:
    for pat in (
        r"[$#]\s?([A-Za-z0-9]{2,15})\b",
        r"(?m)^\s*([A-Z][A-Z0-9]{1,14})\s*(?:/?USDT)?\s*[\(\s]*\d{0,3}\s*[xX]?\s*(?:做多|做空|多|空|long|short)",
        r"\b([A-Z][A-Z0-9]{1,14})/?USDT\b",
        r"(?m)^\s*([A-Z][A-Z0-9]{1,14})\s*\(",
    ):
        for m in re.finditer(pat, text):
            sym = m.group(1).upper()
            if sym.endswith("USDT") and len(sym) > 4:
                sym = sym[:-4]
            if sym not in _SYMBOL_BLACKLIST and not sym.isdigit():
                return sym
    return None


def extract_side(text: str) -> Optional[str]:
    low = text.lower()
    is_short = any(w in low for w in _SHORT_WORDS)
    is_long = any(w in low for w in _LONG_WORDS)
    if not is_short and not is_long:
        if "📉" in text:
            is_short = True
        elif "📈" in text:
            is_long = True
    if is_short == is_long:
        return None
    return SHORT if is_short else LONG


def extract_leverage(text: str) -> Optional[int]:
    m = re.search(r"(\d{1,3})\s*[xX×](?![a-zA-Z])", text)
    if not m:
        m = re.search(r"(\d{1,3})\s*倍\s*(?:做多|做空|多|空)", text)
    if m:
        lev = int(m.group(1))
        if 1 <= lev <= 200:
            return lev
    return None


def _parse_update(text: str, symbol: str) -> Optional[Update]:
    m = re.search(r"(?:提損|提损|移損|移损|止損移至|止损移至|止損上移|止损上移|止損調整|止损调整)\s*:?\s*(" + _NUM + ")", text)
    if m:
        return Update(symbol, "move_sl", price=float(m.group(1)), raw=text)
    if re.search(r"保本|止損.{0,3}開倉價|止损.{0,3}开仓价", text):
        return Update(symbol, "move_sl", price=None, raw=text)   # break-even
    m = re.search(r"(?:減倉|减仓|平倉|平仓|止盈)\s*(\d{1,3})\s*%", text)
    if m:
        return Update(symbol, "reduce", percent=float(m.group(1)), raw=text)
    if re.search(r"全部(?:止盈|平倉|平仓|離場|离场)|全平|清倉|清仓|close all", text, re.IGNORECASE):
        return Update(symbol, "close", raw=text)
    return None


def parse_message(raw: str) -> Parsed:
    """Parse a channel message into a Signal, an Update, or None."""
    if not raw or not raw.strip():
        return None
    text = normalize(raw)
    symbol = extract_symbol(text)
    if not symbol:
        return None

    side = extract_side(text)
    if side is None:
        return _parse_update(text, symbol)

    if not _ACTION_RE.search(text) and extract_leverage(text) is None:
        return None  # chatter like "今天想做空這個"

    sig = Signal(symbol=symbol, side=side, raw=raw)
    sig.market = bool(re.search(r"市[價价]|market", text, re.IGNORECASE))
    sig.leverage = extract_leverage(text)

    entries = _values_after(text, _ENTRY_KEYS)
    if not entries:
        # Bare "96.4-96.8" line right under the header.
        for line in text.splitlines()[1:4]:
            if re.fullmatch(r"\s*" + _NUM + r"(\s*-\s*" + _NUM + r")?\s*", line):
                entries = _numbers(line)
                break
    if not entries:
        # Single-line repost: "#HYPE 輕倉市價多 96.4-96.8 ✅止盈:..." -> numbers right after the side word.
        header = text.splitlines()[0]
        m = re.search(r"(?:" + "|".join(map(re.escape, _SHORT_WORDS + _LONG_WORDS)) + r")"
                      r"\s*(" + _NUM + r"(?:\s*-\s*" + _NUM + r")?)(?=\s|$|[✅❌])", header, re.IGNORECASE)
        if m:
            entries = _numbers(m.group(1))
    if entries:
        entries = entries[:2]
        sig.entry_low, sig.entry_high = min(entries), max(entries)

    sig.take_profits = _values_after(text, _TP_KEYS)
    sls = _values_after(text, _SL_KEYS)
    if sls:
        sig.stop_loss = sls[0]

    return sanitize(sig)


def sanitize(sig: Signal) -> Optional[Signal]:
    """Drop levels that sit on the wrong side of the entry; reject nonsense."""
    ref = sig.entry_ref
    if ref is None:
        return sig
    if sig.side == LONG:
        sig.take_profits = sorted(tp for tp in sig.take_profits if tp > ref)
        if sig.stop_loss is not None and sig.stop_loss >= (sig.entry_low or ref):
            return None
    else:
        sig.take_profits = sorted((tp for tp in sig.take_profits if tp < ref), reverse=True)
        if sig.stop_loss is not None and sig.stop_loss <= (sig.entry_high or ref):
            return None
    return sig
