"""
short_entry.py — полуавтоматический скрипт входа в короткую сделку.
Bear Market Breakdown System для фьючерсов Bybit (Linear Perpetual).

Зеркало trade_entry.py: тот же процесс, тот же риск-менеджмент,
но направление и все ценовые расчёты инвертированы под шорт
(пробитие уровня поддержки вниз вместо пробоя сопротивления вверх).

Использование:
    python short_entry.py SOLUSDT 185.50

Аргументы:
    symbol           — символ, например SOLUSDT, ETHUSDT
    breakdown_level   — ценовой уровень пробития (поддержка)
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal

import pandas as pd
from dotenv import load_dotenv
from pybit.unified_trading import HTTP

from db import init_db, save_trade

# ──────────────────────────────────────────────
# Конфигурация
# ──────────────────────────────────────────────

load_dotenv()

API_KEY    = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")
TESTNET    = os.getenv("TESTNET", "false").lower() == "true"

# Риск на сделку (0.0075 = 0.75% от баланса)
RISK_PCT = float(os.getenv("RISK_PCT", "0.0075"))

# Максимальная доля баланса, отведённая под маржу одной позиции
MAX_MARGIN_PCT = float(os.getenv("MAX_MARGIN_PCT", "0.10"))

# Плечо
LEVERAGE = int(os.getenv("LEVERAGE", "2"))

# Комиссия Bybit за лимитный ордер (одна сторона)
COMMISSION_RATE = 0.00055  # 0.055%

# Анти-памп фильтр: если символ вырос больше чем на этот % за
# SHORT_PUMP_LOOKBACK_HOURS — считаем это pump-and-dump структурой
# (рост без накопления), а не классическим пробоем вниз, и пропускаем сделку.
SHORT_MAX_PUMP_PCT        = float(os.getenv("SHORT_MAX_PUMP_PCT", "0.15"))
SHORT_PUMP_LOOKBACK_HOURS = int(os.getenv("SHORT_PUMP_LOOKBACK_HOURS", "24"))

# ──────────────────────────────────────────────
# Логирование
# ──────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler("short_entry.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════
# Вспомогательные функции — округление
# ══════════════════════════════════════════════

def round_price(price: float, tick_size: float) -> float:
    """Округлить цену вниз до ближайшего допустимого тика."""
    tick = Decimal(str(tick_size))
    p    = Decimal(str(price))
    return float((p / tick).to_integral_value(rounding=ROUND_DOWN) * tick)


def round_price_up(price: float, tick_size: float) -> float:
    """
    Округлить цену вверх до ближайшего допустимого тика.

    Для шорта — зеркало round_price(): там, где длинная сторона
    "консервативно" округляет вниз (чтобы не переплатить на входе
    и не продешевить на тейке), короткая сторона консервативно
    округляет вверх (не продать вход слишком дёшево, не купить
    закрытие слишком дорого).

    Перед округлением цена чуть сдвигается вниз на относительную
    погрешность double (~1e-9 от значения) — иначе значение, которое
    математически ровно совпадает с границей тика, но из-за шума
    в последних разрядах float лежит на ничтожную долю выше неё,
    ROUND_UP гарантированно перебрасывает на следующий тик.
    round_price() (округление вниз) с этой же погрешностью не страдает —
    там ROUND_DOWN в такой ситуации просто остаётся на границе.
    """
    tick = Decimal(str(tick_size))
    p    = Decimal(str(price - abs(price) * 1e-9))
    return float((p / tick).to_integral_value(rounding=ROUND_UP) * tick)


def round_qty(qty: float, qty_step: float) -> float:
    """Округлить количество вниз до ближайшего кратного шагу лота."""
    step   = Decimal(str(qty_step))
    amount = Decimal(str(qty))
    return float((amount / step).to_integral_value(rounding=ROUND_DOWN) * step)


def fmt(value: float) -> str:
    """Конвертировать float в строку без научной нотации и лишних нулей."""
    d = Decimal(str(value))
    n = d.normalize()
    if "E" in str(n):
        return str(d)
    return str(n)


# ══════════════════════════════════════════════
# API — создание сессии
# ══════════════════════════════════════════════

def create_session() -> HTTP:
    """Инициализировать HTTP-сессию Bybit с учётом режима testnet."""
    if not API_KEY or not API_SECRET:
        log.error("BYBIT_API_KEY и BYBIT_API_SECRET не заданы в .env")
        sys.exit(1)
    return HTTP(testnet=TESTNET, api_key=API_KEY, api_secret=API_SECRET)


# ══════════════════════════════════════════════
# API — получение рыночных данных
# ══════════════════════════════════════════════

def get_balance(session: HTTP) -> float:
    """Получить суммарный капитал (equity) Unified аккаунта в USDT."""
    resp = session.get_wallet_balance(accountType="UNIFIED")
    if resp["retCode"] != 0:
        raise RuntimeError(f"Баланс: {resp['retMsg']}")

    account = resp["result"]["list"][0]
    return float(account["totalEquity"])


def get_instrument_info(session: HTTP, symbol: str) -> dict:
    """Получить параметры инструмента: tick_size, qty_step, min_qty, max_qty."""
    resp = session.get_instruments_info(category="linear", symbol=symbol)
    if resp["retCode"] != 0:
        raise RuntimeError(f"Инструмент {symbol}: {resp['retMsg']}")

    info       = resp["result"]["list"][0]
    lot_filter = info["lotSizeFilter"]
    px_filter  = info["priceFilter"]

    return {
        "tick_size": float(px_filter["tickSize"]),
        "qty_step":  float(lot_filter["qtyStep"]),
        "min_qty":   float(lot_filter["minOrderQty"]),
        "max_qty":   float(lot_filter.get("maxOrderQty", 9_999_999)),
    }


def get_klines(session: HTTP, symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
    """Загрузить OHLCV-свечи с Bybit (старые → новые)."""
    resp = session.get_kline(
        category="linear",
        symbol=symbol,
        interval=interval,
        limit=limit,
    )
    if resp["retCode"] != 0:
        raise RuntimeError(f"Свечи {symbol}/{interval}: {resp['retMsg']}")

    candles = resp["result"]["list"][::-1]

    df = pd.DataFrame(
        candles,
        columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
    )
    df = df.astype({
        "timestamp": "int64",
        "open":      "float64",
        "high":      "float64",
        "low":       "float64",
        "close":     "float64",
        "volume":    "float64",
        "turnover":  "float64",
    })
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df


def get_ticker_data(session: HTTP, symbol: str) -> dict:
    """Получить данные тикера: цена, 24H объём, OI, bid/ask для спреда."""
    resp = session.get_tickers(category="linear", symbol=symbol)
    if resp["retCode"] != 0:
        raise RuntimeError(f"Тикер {symbol}: {resp['retMsg']}")

    t   = resp["result"]["list"][0]
    bid = float(t["bid1Price"]) if t.get("bid1Price") else 0.0
    ask = float(t["ask1Price"]) if t.get("ask1Price") else 0.0

    spread_pct = (ask - bid) / ask * 100 if ask > 0 else 999.0

    return {
        "last_price":      float(t["lastPrice"]),
        "volume_24h_usd":  float(t["turnover24h"]),
        "open_interest":   float(t.get("openInterestValue", 0)),
        "bid":             bid,
        "ask":             ask,
        "spread_pct":      spread_pct,
    }


def get_funding_rate(session: HTTP, symbol: str) -> float:
    """Получить последний реализованный funding rate (за 8ч)."""
    resp = session.get_funding_rate_history(
        category="linear",
        symbol=symbol,
        limit=1,
    )
    if resp["retCode"] != 0:
        raise RuntimeError(f"Funding rate {symbol}: {resp['retMsg']}")

    lst = resp["result"]["list"]
    return float(lst[0]["fundingRate"]) if lst else 0.0


# ══════════════════════════════════════════════
# Технический анализ
# ══════════════════════════════════════════════

def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    """ATR по методу Уайлдера (EMA с alpha = 1/period)."""
    high  = df["high"]
    low   = df["low"]
    prev_close = df["close"].shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr_series = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    return float(atr_series.iloc[-1])


def calculate_ema(series: pd.Series, period: int) -> pd.Series:
    """EMA стандартный (adjust=False соответствует большинству платформ)."""
    return series.ewm(span=period, adjust=False).mean()


def find_swing_highs(highs: pd.Series, window: int = 3) -> list[float]:
    """
    Найти swing high: максимум свечи строго выше window соседних свечей
    с каждой стороны. Зеркало find_swing_lows() из trade_entry.py.
    Возвращает значения последних 4 swing high в хронологическом порядке.
    """
    values = highs.values
    result = []

    for i in range(window, len(values) - window):
        left_ok  = all(values[i] > values[i - j] for j in range(1, window + 1))
        right_ok = all(values[i] > values[i + j] for j in range(1, window + 1))
        if left_ok and right_ok:
            result.append(float(values[i]))

    return result[-4:]


# ══════════════════════════════════════════════
# Фильтры
# ══════════════════════════════════════════════

def check_btc_bear_filter(session: HTTP) -> tuple[bool, str]:
    """
    Проверить медвежье состояние рынка BTC — зеркало check_btc_filter():

    1. Цена BTC ниже EMA50 и EMA200 на 4H (медвежий тренд)
    2. Последние два swing high 4H ниже предыдущих (lower highs)
    3. Ни одна из трёх последних 1H свечей не выросла на +2.5% и более
       (защита от шорт-сквиза в момент входа)
    """
    # --- Условие 1: EMA50 / EMA200 на 4H ---
    df_4h = get_klines(session, "BTCUSDT", "240", limit=250)

    if len(df_4h) < 200:
        return False, "Недостаточно 4H-свечей BTC для расчёта EMA200"

    close_4h  = df_4h["close"]
    btc_price = float(close_4h.iloc[-1])

    ema50  = float(calculate_ema(close_4h, 50).iloc[-1])
    ema200 = float(calculate_ema(close_4h, 200).iloc[-1])

    if btc_price >= ema50:
        return False, f"BTC ${btc_price:,.0f} выше EMA50 ${ema50:,.0f} на 4H"
    if btc_price >= ema200:
        return False, f"BTC ${btc_price:,.0f} выше EMA200 ${ema200:,.0f} на 4H"

    # --- Условие 2: lower highs ---
    swing_highs = find_swing_highs(df_4h["high"], window=3)

    if len(swing_highs) < 3:
        return False, f"Мало swing high для анализа (найдено {len(swing_highs)}, нужно ≥3)"

    if swing_highs[-1] >= swing_highs[-2]:
        return (
            False,
            f"Последний swing high {swing_highs[-1]:,.0f} не ниже предыдущего {swing_highs[-2]:,.0f}",
        )
    if swing_highs[-2] >= swing_highs[-3]:
        return (
            False,
            f"Swing high[-2] {swing_highs[-2]:,.0f} не ниже swing high[-3] {swing_highs[-3]:,.0f}",
        )

    # --- Условие 3: нет шорт-сквиза на 1H за последние 3 часа ---
    df_1h = get_klines(session, "BTCUSDT", "60", limit=5)
    for _, candle in df_1h.tail(3).iterrows():
        change = (candle["close"] - candle["open"]) / candle["open"]
        if change >= 0.025:
            return False, f"BTC рывок вверх {change:+.1%} на 1H за последние 3 часа"

    msg = (
        f"BTC OK: ${btc_price:,.0f} < EMA50(${ema50:,.0f}) < EMA200(${ema200:,.0f}), "
        f"lower highs подтверждены"
    )
    return True, msg


def check_no_recent_pump(session: HTTP, symbol: str) -> tuple[bool, str]:
    """
    Анти-памп фильтр по самому символу (не по BTC).

    Классический пробой вниз происходит после дистрибуции/боковика:
    цена долго формирует уровень, затем его пробивает. Pump-and-dump —
    другая структура: резкий вертикальный рост без накопления, а затем
    такое же резкое падение. Такой откат легко перепутать с "пробоем",
    но входить в него так же рискованно, как гнаться за уже начавшимся
    движением — стоп по ATR в этот момент искусственно расширен самим
    пампом, а не отражает реальную волатильность инструмента.

    Проверяем рост цены символа за последние SHORT_PUMP_LOOKBACK_HOURS
    часов: если он превышает SHORT_MAX_PUMP_PCT — считаем сетап пампом
    и блокируем вход, независимо от того, что говорят остальные фильтры.
    """
    limit = SHORT_PUMP_LOOKBACK_HOURS + 1
    df = get_klines(session, symbol, "60", limit=limit)

    if len(df) < limit:
        return False, (
            f"Недостаточно 1H-свечей {symbol} для анти-памп проверки "
            f"({len(df)} < {limit})"
        )

    price_then = float(df["close"].iloc[0])
    price_now  = float(df["close"].iloc[-1])
    pump_pct   = (price_now - price_then) / price_then

    if pump_pct > SHORT_MAX_PUMP_PCT:
        return False, (
            f"Памп {pump_pct:+.1%} за {SHORT_PUMP_LOOKBACK_HOURS}ч "
            f"> порога {SHORT_MAX_PUMP_PCT:.0%} — похоже на pump-and-dump, "
            f"не классический пробой"
        )

    return True, (
        f"Без признаков пампа: {pump_pct:+.1%} за {SHORT_PUMP_LOOKBACK_HOURS}ч "
        f"(порог {SHORT_MAX_PUMP_PCT:.0%})"
    )


# ══════════════════════════════════════════════
# Управление ордерами
# ══════════════════════════════════════════════

def set_leverage(session: HTTP, symbol: str) -> None:
    """Установить плечо для символа. Ошибка игнорируется, если плечо уже задано."""
    resp = session.set_leverage(
        category="linear",
        symbol=symbol,
        buyLeverage=str(LEVERAGE),
        sellLeverage=str(LEVERAGE),
    )
    if resp["retCode"] not in (0, 110043):
        raise RuntimeError(f"Установка плеча: {resp['retMsg']}")


def place_limit_order(
    session: HTTP,
    symbol: str,
    side: str,
    qty: float,
    price: float,
    reduce_only: bool = False,
) -> str:
    """Выставить лимитный ордер GTC. side: 'Buy' или 'Sell'."""
    resp = session.place_order(
        category="linear",
        symbol=symbol,
        side=side,
        orderType="Limit",
        qty=fmt(qty),
        price=fmt(price),
        timeInForce="GTC",
        reduceOnly=reduce_only,
    )
    if resp["retCode"] != 0:
        raise RuntimeError(f"Лимитный ордер {side} {symbol}: {resp['retMsg']}")

    return resp["result"]["orderId"]


def wait_for_fill(
    session: HTTP,
    symbol: str,
    order_id: str,
    timeout: int = 300,
    poll_interval: int = 5,
) -> tuple[float, float]:
    """
    Ожидать исполнения ордера до timeout секунд.
    Возвращает (filled_qty, avg_price); при таймауте отменяет остаток.
    """
    log.info(f"Ожидание ордера {order_id} (макс. {timeout}с)...")
    deadline = time.time() + timeout

    while time.time() < deadline:
        open_resp = session.get_open_orders(
            category="linear",
            symbol=symbol,
            orderId=order_id,
        )

        if open_resp["retCode"] == 0 and open_resp["result"]["list"]:
            o          = open_resp["result"]["list"][0]
            status     = o["orderStatus"]
            filled_qty = float(o.get("cumExecQty") or 0)
            avg_price  = float(o.get("avgPrice") or 0)

            log.info(f"  [{status}] заполнено={filled_qty} @ {avg_price}")

            if status == "Filled":
                return filled_qty, avg_price
        else:
            hist = session.get_order_history(
                category="linear",
                symbol=symbol,
                orderId=order_id,
                limit=1,
            )
            if hist["retCode"] == 0 and hist["result"]["list"]:
                o          = hist["result"]["list"][0]
                filled_qty = float(o.get("cumExecQty") or 0)
                avg_price  = float(o.get("avgPrice") or 0)
                log.info(f"  История: [{o['orderStatus']}] filled={filled_qty}")
                return filled_qty, avg_price

        time.sleep(poll_interval)

    log.warning(f"Таймаут. Отмена остатка ордера {order_id}...")
    session.cancel_order(category="linear", symbol=symbol, orderId=order_id)
    time.sleep(2)

    hist = session.get_order_history(
        category="linear",
        symbol=symbol,
        orderId=order_id,
        limit=1,
    )
    if hist["retCode"] == 0 and hist["result"]["list"]:
        o          = hist["result"]["list"][0]
        filled_qty = float(o.get("cumExecQty") or 0)
        avg_price  = float(o.get("avgPrice") or 0)
        return filled_qty, avg_price

    return 0.0, 0.0


def set_stop_loss(session: HTTP, symbol: str, stop_price: float, tick_size: float) -> None:
    """
    Установить позиционный стоп-лосс через set_trading_stop.
    Направление (выше/ниже входа) определяется биржей автоматически
    по стороне позиции — функция не зависит от long/short.
    """
    price_str = fmt(round_price_up(stop_price, tick_size))
    resp = session.set_trading_stop(
        category="linear",
        symbol=symbol,
        stopLoss=price_str,
        slTriggerBy="LastPrice",
        positionIdx=0,  # one-way mode
    )
    if resp["retCode"] != 0:
        raise RuntimeError(f"Стоп-лосс {symbol}: {resp['retMsg']}")

    log.info(f"Стоп-лосс установлен @ {price_str}")


# ══════════════════════════════════════════════
# Главная функция
# ══════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bear Market Breakdown — вход в короткую сделку"
    )
    parser.add_argument("symbol",          type=str,   help="Символ (например SOLUSDT)")
    parser.add_argument("breakdown_level", type=float, help="Уровень пробития (поддержка)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Прогнать фильтры и расчёт плана на живых данных, но не размещать ордер",
    )
    args = parser.parse_args()

    symbol          = args.symbol.upper()
    breakdown_level = args.breakdown_level

    log.info(f"═══ Запуск: {symbol} | уровень пробития = {breakdown_level} ═══")

    session = create_session()
    init_db()

    # ══════════════════════════════════════════
    # Блок 1: Получение базовых данных
    # ══════════════════════════════════════════

    print(f"\n{'═'*62}")
    print(f"  BEAR MARKET BREAKDOWN  |  {symbol}  @  {breakdown_level}")
    print(f"{'═'*62}")
    print("\nЗагрузка данных...")

    try:
        balance = get_balance(session)
    except Exception as e:
        log.error(f"Баланс: {e}")
        sys.exit(1)

    try:
        instr     = get_instrument_info(session, symbol)
        tick_size = instr["tick_size"]
        qty_step  = instr["qty_step"]
        min_qty   = instr["min_qty"]
    except Exception as e:
        log.error(f"Инструмент: {e}")
        sys.exit(1)

    try:
        ticker        = get_ticker_data(session, symbol)
        current_price = ticker["last_price"]
        volume_24h    = ticker["volume_24h_usd"]
        open_interest = ticker["open_interest"]
        spread_pct    = ticker["spread_pct"]
    except Exception as e:
        log.error(f"Тикер: {e}")
        sys.exit(1)

    try:
        df_1h = get_klines(session, symbol, "60", limit=50)
        atr   = calculate_atr(df_1h, period=14)
    except Exception as e:
        log.error(f"ATR: {e}")
        sys.exit(1)

    try:
        funding_rate = get_funding_rate(session, symbol)
    except Exception as e:
        log.warning(f"Funding rate: {e}")
        funding_rate = 0.0

    print(f"  Баланс:         ${balance:>12,.2f}")
    print(f"  Цена {symbol:<10} ${current_price:>12,.4f}")
    print(f"  ATR(14, 1H):    {atr:>12,.4f}  ({atr / current_price * 100:.2f}%)")
    print(f"  Тик-сайз:       {tick_size}  |  Шаг лота: {qty_step}  |  Мин. лот: {min_qty}")

    # ══════════════════════════════════════════
    # Блок 2: Фильтры входа
    # ══════════════════════════════════════════

    print(f"\n{'─'*62}")
    print("  ФИЛЬТРЫ")
    print(f"{'─'*62}")

    all_ok = True

    # Фильтр 1: BTC медвежий режим
    try:
        btc_ok, btc_msg = check_btc_bear_filter(session)
    except Exception as e:
        btc_ok, btc_msg = False, f"Ошибка проверки BTC: {e}"

    mark = "✓" if btc_ok else "✗"
    print(f"  [{mark}] BTC-фильтр: {btc_msg}")
    if not btc_ok:
        all_ok = False

    # Фильтр 2: 24H объём
    vol_ok = volume_24h >= 50_000_000
    mark   = "✓" if vol_ok else "✗"
    print(f"  [{mark}] Объём 24H:   ${volume_24h / 1e6:,.1f}M  (мин: $50M)")
    if not vol_ok:
        all_ok = False

    # Фильтр 3: Open Interest
    oi_ok = open_interest >= 20_000_000
    mark  = "✓" if oi_ok else "✗"
    print(f"  [{mark}] Open Interest: ${open_interest / 1e6:,.1f}M  (мин: $20M)")
    if not oi_ok:
        all_ok = False

    # Фильтр 4: Спред
    spread_ok = spread_pct < 0.1
    mark      = "✓" if spread_ok else "✗"
    print(f"  [{mark}] Спред:       {spread_pct:.4f}%  (макс: 0.1%)")
    if not spread_ok:
        all_ok = False

    # Фильтр 5: Funding rate
    fr_pct = abs(funding_rate)
    fr_ok  = fr_pct <= 0.0005
    mark   = "✓" if fr_ok else "✗"
    print(f"  [{mark}] Funding rate: {fr_pct:.4%}  (макс: 0.05%)")
    if not fr_ok:
        all_ok = False

    # Фильтр 6: анти-памп (по символу, не по BTC)
    try:
        pump_ok, pump_msg = check_no_recent_pump(session, symbol)
    except Exception as e:
        pump_ok, pump_msg = False, f"Ошибка анти-памп проверки: {e}"

    mark = "✓" if pump_ok else "✗"
    print(f"  [{mark}] Анти-памп:   {pump_msg}")
    if not pump_ok:
        all_ok = False

    # ══════════════════════════════════════════
    # Блок 3: Расчёт параметров сделки
    # ══════════════════════════════════════════

    print(f"\n{'─'*62}")
    print("  РАСЧЁТ СДЕЛКИ")
    print(f"{'─'*62}")

    # Цена входа: лимитный ордер чуть ниже уровня пробития
    entry_price = round_price_up(breakdown_level - 0.1 * atr, tick_size)

    # Стоп-лосс — наиболее консервативный (самый широкий) из трёх вариантов.
    # Для шорта стоп выше входа, поэтому берём максимум (зеркало min() у лонга).
    stop_v1 = breakdown_level + 0.5 * atr       # над уровнем пробития
    stop_v2 = entry_price     + 2.0 * atr        # широкий ATR-стоп от входа
    stop_v3 = entry_price     * 1.06             # фиксированный +6%
    stop_price = round_price_up(max(stop_v1, stop_v2, stop_v3), tick_size)

    stop_distance     = stop_price - entry_price
    stop_distance_pct = stop_distance / entry_price

    print(f"  Вход (лимит):   ${entry_price:.4f}")
    print(f"  Стоп:")
    print(f"    v1 breakdown + 0.5·ATR = ${stop_v1:.4f}")
    print(f"    v2 entry   + 2·ATR     = ${stop_v2:.4f}")
    print(f"    v3 entry   × 1.06      = ${stop_v3:.4f}")
    print(f"    → выбран максимум:       ${stop_price:.4f}  ({stop_distance_pct:.1%})")

    stop_width_ok = 0.06 <= stop_distance_pct <= 0.15
    if stop_distance_pct > 0.15:
        print(f"  [✗] Стоп слишком широкий: {stop_distance_pct:.1%} > 15%")
        all_ok = False
    elif stop_distance_pct < 0.06:
        print(f"  [✗] Стоп слишком узкий:   {stop_distance_pct:.1%} < 6%")
        all_ok = False
    else:
        print(f"  [✓] Ширина стопа: {stop_distance_pct:.1%}  (норма: 6–15%)")

    # R-единица = расстояние стоп-лосса
    R = stop_distance

    # Тейк-профиты — ниже входа
    tp1 = round_price_up(entry_price - 2.0 * R, tick_size)   # +2R
    tp2 = round_price_up(entry_price - 4.0 * R, tick_size)   # +4R

    print(f"  TP1 (+2R):      ${tp1:.4f}")
    print(f"  TP2 (+4R):      ${tp2:.4f}")

    # Размер позиции от риска 0.75% баланса
    risk_amount = balance * RISK_PCT
    qty_raw     = risk_amount / stop_distance
    qty_total   = round_qty(qty_raw, qty_step)

    position_notional = qty_total * entry_price
    required_margin   = position_notional / LEVERAGE
    margin_pct        = required_margin / balance

    print(f"\n  Риск:           ${risk_amount:.2f}  ({RISK_PCT:.2%} баланса)")
    print(f"  Размер:         {qty_total} контрактов")
    print(f"  Условная ст-ть: ${position_notional:,.2f}")
    print(f"  Маржа (x{LEVERAGE}):    ${required_margin:,.2f}  ({margin_pct:.1%} баланса)")

    if qty_total < min_qty:
        print(f"  [✗] Размер {qty_total} < мин. лот {min_qty}")
        all_ok = False
        qty_total = 0.0
    else:
        print(f"  [✓] Мин. лот: {qty_total} ≥ {min_qty}")

    if margin_pct > MAX_MARGIN_PCT:
        print(f"  [✗] Маржа {margin_pct:.1%} > {MAX_MARGIN_PCT:.0%} баланса")
        all_ok = False
    elif qty_total > 0:
        print(f"  [✓] Маржа: {margin_pct:.1%} ≤ {MAX_MARGIN_PCT:.0%}")

    # Разбивка на части: TP1 (30%), TP2 (30%), runner (40%)
    qty_tp1    = round_qty(qty_total * 0.30, qty_step) if qty_total > 0 else 0.0
    qty_tp2    = round_qty(qty_total * 0.30, qty_step) if qty_total > 0 else 0.0
    qty_runner = qty_total - qty_tp1 - qty_tp2         if qty_total > 0 else 0.0

    if qty_total > 0 and qty_tp1 < min_qty:
        print(f"  [✗] 30% qty = {qty_tp1} < мин. лот {min_qty} — нельзя разбить на части")
        all_ok = False
    elif qty_total > 0:
        print(f"  [✓] Разбивка: TP1={qty_tp1} | TP2={qty_tp2} | Runner={qty_runner:.4g}")

    # Проверка: цена не ушла дальше 1.5·ATR ниже уровня пробития (не гонимся за дампом)
    price_dist = breakdown_level - current_price
    max_dist   = 1.5 * atr
    if price_dist > max_dist:
        print(
            f"  [✗] Цена ушла на {price_dist:.4f} > 1.5·ATR ({max_dist:.4f}) "
            f"ниже пробития — не гонимся"
        )
        all_ok = False
    else:
        dist_label = f"+{price_dist:.4f}" if price_dist >= 0 else f"{price_dist:.4f}"
        print(f"  [✓] Расстояние от пробития: {dist_label}  (лимит: {max_dist:.4f})")

    trailing_pct = max(2.5 * atr / entry_price, 0.10)

    commission = position_notional * COMMISSION_RATE * 2

    # ══════════════════════════════════════════
    # Блок 4: Сводный план сделки
    # ══════════════════════════════════════════

    print(f"\n{'═'*62}")
    print(f"  ПЛАН СДЕЛКИ")
    print(f"{'═'*62}")
    print(f"  Символ:           {symbol}")
    print(f"  Направление:      SHORT")
    print(f"  Уровень пробития: ${breakdown_level}")
    print(f"  Вход (лимит):     ${entry_price:.4f}")
    print(f"  Стоп-лосс:        ${stop_price:.4f}  (+{stop_distance_pct:.1%})")
    print(f"  TP1 (+2R):        ${tp1:.4f}  →  qty {qty_tp1} (30%)")
    print(f"  TP2 (+4R):        ${tp2:.4f}  →  qty {qty_tp2} (30%)")
    print(f"  Runner:                         qty {qty_runner:.4g} (40%)")
    print(f"  Трейлинг-стоп:    {trailing_pct:.2%}  (предварительно)")
    print(f"  Плечо:            x{LEVERAGE}")
    print(f"  Маржа:            ${required_margin:,.2f}")
    print(f"  Комиссия (~):     ${commission:.2f}  (0.055% × 2)")
    print(f"  Риск:             ${risk_amount:.2f}  ({RISK_PCT:.2%})")
    print(f"  Цель TP1 (+2R):   ${risk_amount * 2:.2f}")
    print(f"  Цель TP2 (+4R):   ${risk_amount * 4:.2f}")
    print(f"{'═'*62}")

    if not all_ok:
        print("\n  [!] Один или несколько фильтров не пройдены. Торговля невозможна.")
        log.warning("Сделка отклонена: не пройдены фильтры")
        sys.exit(0)

    if args.dry_run:
        print("\n  [DRY-RUN] Ордер не размещается. План сделки выше рассчитан на живых данных.")
        log.info("Dry-run: план рассчитан, ордер не размещён")
        sys.exit(0)

    # ══════════════════════════════════════════
    # Блок 5: Подтверждение пользователя
    # ══════════════════════════════════════════

    print("\n  Все фильтры пройдены.")
    try:
        confirm = input("  Разместить ордер? [y/n]: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print("\nОтменено.")
        sys.exit(0)

    if confirm != "y":
        print("  Отменено пользователем.")
        sys.exit(0)

    # ══════════════════════════════════════════
    # Блок 6: Обновление данных в момент входа
    # ══════════════════════════════════════════

    print("\n  Обновление данных перед размещением ордера...")

    ticker_fresh = get_ticker_data(session, symbol)
    fresh_price  = ticker_fresh["last_price"]

    df_1h_fresh = get_klines(session, symbol, "60", limit=50)
    atr_fresh   = calculate_atr(df_1h_fresh, period=14)

    entry_fresh  = round_price_up(breakdown_level - 0.1 * atr_fresh, tick_size)
    trailing_pct = max(2.5 * atr_fresh / entry_fresh, 0.10)

    log.info(
        f"Свежие данные: цена={fresh_price:.4f} ATR={atr_fresh:.4f} "
        f"entry={entry_fresh:.4f} trailing={trailing_pct:.2%}"
    )

    fresh_dist = breakdown_level - fresh_price
    if fresh_dist > 1.5 * atr_fresh:
        print(
            f"  [!] Цена ушла на {fresh_dist:.4f} > 1.5·ATR ({1.5 * atr_fresh:.4f})."
            " Сделка отменена."
        )
        sys.exit(0)

    # ══════════════════════════════════════════
    # Блок 7: Размещение ордера
    # ══════════════════════════════════════════

    try:
        set_leverage(session, symbol)
    except Exception as e:
        log.warning(f"Плечо: {e}")

    print(f"\n  Размещение лимитного ордера: {qty_total} × {symbol} @ ${entry_fresh:.4f}...")

    try:
        order_id = place_limit_order(session, symbol, "Sell", qty_total, entry_fresh)
        log.info(f"Ордер размещён: {order_id}")
        print(f"  ID ордера: {order_id}")
    except Exception as e:
        log.error(f"Размещение ордера: {e}")
        sys.exit(1)

    # ══════════════════════════════════════════
    # Блок 8: Ожидание исполнения (макс. 5 мин)
    # ══════════════════════════════════════════

    print("  Ожидание исполнения (макс. 5 минут)...")
    filled_qty, avg_fill = wait_for_fill(session, symbol, order_id, timeout=300)

    if filled_qty <= 0:
        print("  [!] Ордер не исполнен. Сделка пропущена.")
        log.warning("Нулевое исполнение — сделка пропущена")
        sys.exit(0)

    log.info(f"Исполнено: qty={filled_qty} @ {avg_fill:.4f}")
    print(f"\n  ✓ Исполнено: {filled_qty} контрактов @ ${avg_fill:.4f}")

    # ══════════════════════════════════════════
    # Блок 9: Пересчёт с реальной ценой входа
    # ══════════════════════════════════════════

    actual_entry = avg_fill

    stop_a1 = breakdown_level + 0.5 * atr_fresh
    stop_a2 = actual_entry    + 2.0 * atr_fresh
    stop_a3 = actual_entry    * 1.06
    actual_stop     = round_price_up(max(stop_a1, stop_a2, stop_a3), tick_size)
    actual_stop_pct = (actual_stop - actual_entry) / actual_entry

    actual_R   = actual_stop - actual_entry
    actual_tp1 = round_price_up(actual_entry - 2.0 * actual_R, tick_size)
    actual_tp2 = round_price_up(actual_entry - 4.0 * actual_R, tick_size)

    qty_tp1_a    = round_qty(filled_qty * 0.30, qty_step)
    qty_tp2_a    = round_qty(filled_qty * 0.30, qty_step)
    qty_runner_a = filled_qty - qty_tp1_a - qty_tp2_a

    # ══════════════════════════════════════════
    # Блок 10: Выставление защитных ордеров
    # ══════════════════════════════════════════

    print(f"  Установка стоп-лосса @ ${actual_stop:.4f}...")
    try:
        set_stop_loss(session, symbol, actual_stop, tick_size)
    except Exception as e:
        log.critical(f"СТОП-ЛОСС НЕ УСТАНОВЛЕН: {e}")
        print(f"\n  [!!!] КРИТИЧНО: стоп-лосс не установлен: {e}")
        print("  Немедленно установите стоп вручную!")

    # TP1 — лимитный ордер (Buy, закрытие 30% шорта)
    tp1_order_id = None
    if qty_tp1_a >= min_qty:
        print(f"  Размещение TP1 @ ${actual_tp1:.4f}  qty={qty_tp1_a}...")
        try:
            tp1_order_id = place_limit_order(
                session, symbol, "Buy", qty_tp1_a, actual_tp1, reduce_only=True
            )
            log.info(f"TP1 ордер: {tp1_order_id}")
        except Exception as e:
            log.error(f"TP1: {e}")
            print(f"  [!] TP1 не выставлен: {e}")
    else:
        print(f"  [!] TP1 пропущен: qty_tp1={qty_tp1_a} < мин. лот {min_qty}")

    # TP2 — лимитный ордер (Buy, закрытие ещё 30% шорта)
    tp2_order_id = None
    if qty_tp2_a >= min_qty:
        print(f"  Размещение TP2 @ ${actual_tp2:.4f}  qty={qty_tp2_a}...")
        try:
            tp2_order_id = place_limit_order(
                session, symbol, "Buy", qty_tp2_a, actual_tp2, reduce_only=True
            )
            log.info(f"TP2 ордер: {tp2_order_id}")
        except Exception as e:
            log.error(f"TP2: {e}")
            print(f"  [!] TP2 не выставлен: {e}")
    else:
        print(f"  [!] TP2 пропущен: qty_tp2={qty_tp2_a} < мин. лот {min_qty}")

    # ══════════════════════════════════════════
    # Блок 11: Запись в базу данных
    # ══════════════════════════════════════════

    now = datetime.now()
    trade_row = {
        "date":               now.strftime("%Y-%m-%d"),
        "time":               now.strftime("%H:%M:%S"),
        "symbol":             symbol,
        "direction":          "short",
        "entry_price":        actual_entry,
        "breakout_level":     breakdown_level,
        "atr":                atr_fresh,
        "stop_price":         actual_stop,
        "stop_distance_pct":  actual_stop_pct,
        "position_notional":  filled_qty * actual_entry,
        "leverage":           LEVERAGE,
        "required_margin":    (filled_qty * actual_entry) / LEVERAGE,
        "risk_amount":        risk_amount,
        "risk_pct":           RISK_PCT,
        "tp1":                actual_tp1,
        "tp2":                actual_tp2,
        "trailing_pct":       trailing_pct,
        "qty_total":          filled_qty,
        "qty_tp1":            qty_tp1_a,
        "qty_tp2":            qty_tp2_a,
        "qty_runner":         qty_runner_a,
        "btc_regime":         btc_msg,
        "volume_24h":         volume_24h,
        "open_interest":      open_interest,
        "spread":             spread_pct,
        "funding_rate":       funding_rate,
        "entry_order_id":     order_id,
        "status":             "open",
        "created_at":         now.isoformat(),
        "updated_at":         now.isoformat(),
    }

    trade_id = save_trade(trade_row)
    log.info(f"Сделка записана в БД, ID={trade_id}")

    # ══════════════════════════════════════════
    # Итоговое резюме
    # ══════════════════════════════════════════

    print(f"\n{'═'*62}")
    print(f"  СДЕЛКА ОТКРЫТА (SHORT)  |  ID в базе данных: {trade_id}")
    print(f"{'═'*62}")
    print(f"  Символ:         {symbol}")
    print(f"  Вход:           ${actual_entry:.4f}")
    print(f"  Стоп:           ${actual_stop:.4f}  (+{actual_stop_pct:.1%})")
    print(f"  TP1 (+2R):      ${actual_tp1:.4f}  qty={qty_tp1_a}")
    print(f"  TP2 (+4R):      ${actual_tp2:.4f}  qty={qty_tp2_a}")
    print(f"  Runner:                          qty={qty_runner_a:.4g}")
    print(f"  Трейлинг:       {trailing_pct:.2%}")
    print(f"  Риск:           ${risk_amount:.2f}")
    print(f"\n  Запустите: python position_monitor.py {trade_id}")
    print(f"{'═'*62}\n")


if __name__ == "__main__":
    main()
