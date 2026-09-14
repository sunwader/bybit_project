"""
position_monitor.py — мониторинг открытой позиции через WebSocket Bybit.
Bull Market Breakout System.

Запускается после trade_entry.py. Работает непрерывно до закрытия позиции.

Использование:
    python position_monitor.py [trade_id]

Если trade_id не указан — берётся последняя открытая сделка из БД.
"""

import argparse
import logging
import os
import sys
import time
import threading
from decimal import ROUND_DOWN, Decimal

import pandas as pd
from dotenv import load_dotenv
from pybit.unified_trading import HTTP, WebSocket

from db import get_trade_by_id, get_open_trade, update_trade

# ──────────────────────────────────────────────
# Конфигурация
# ──────────────────────────────────────────────

load_dotenv()

API_KEY    = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")
TESTNET    = os.getenv("TESTNET", "false").lower() == "true"

# Пороговое значение funding rate за 8-часовой период
FUNDING_THRESHOLD = 0.0005       # 0.05%

# Принудительно закрыть runner если funding выше порога столько часов подряд
FUNDING_MAX_HOURS = 48.0

# ──────────────────────────────────────────────
# Логирование
# ──────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler("position_monitor.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Фазы жизненного цикла сделки (state machine)
# ──────────────────────────────────────────────

PHASE_WATCHING_TP1 = "watching_tp1"  # исходное состояние: ждём TP1
PHASE_WATCHING_TP2 = "watching_tp2"  # TP1 взят, трейлинг 15%, ждём TP2
PHASE_RUNNER       = "runner"         # TP2 взят, трейлинг сужен до ATR
PHASE_CLOSED       = "closed"         # позиция полностью закрыта


# ══════════════════════════════════════════════
# Утилиты (продублированы из trade_entry.py
#          для независимости модуля)
# ══════════════════════════════════════════════

def _create_session() -> HTTP:
    """Создать HTTP-сессию Bybit."""
    if not API_KEY or not API_SECRET:
        log.error("BYBIT_API_KEY / BYBIT_API_SECRET не заданы в .env")
        sys.exit(1)
    return HTTP(testnet=TESTNET, api_key=API_KEY, api_secret=API_SECRET)


def _round_price(price: float, tick_size: float) -> float:
    """Округлить цену вниз до ближайшего тика."""
    tick = Decimal(str(tick_size))
    p    = Decimal(str(price))
    return float((p / tick).to_integral_value(rounding=ROUND_DOWN) * tick)


def _fmt(value: float) -> str:
    """Float → строка без научной нотации (для Bybit API)."""
    d = Decimal(str(value))
    n = d.normalize()
    return str(d) if "E" in str(n) else str(n)


def _get_klines(session: HTTP, symbol: str, interval: str, limit: int = 50) -> pd.DataFrame:
    """Загрузить OHLCV-свечи в хронологическом порядке (старые → новые)."""
    resp = session.get_kline(
        category="linear",
        symbol=symbol,
        interval=interval,
        limit=limit,
    )
    if resp["retCode"] != 0:
        raise RuntimeError(f"Свечи {symbol}/{interval}: {resp['retMsg']}")

    candles = resp["result"]["list"][::-1]   # Bybit отдаёт в обратном порядке
    df = pd.DataFrame(
        candles,
        columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
    )
    df = df.astype({k: "float64" for k in ["open", "high", "low", "close", "volume"]})
    return df


def _calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    """ATR по методу Уайлдера (EWM с alpha = 1/period)."""
    high       = df["high"]
    low        = df["low"]
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return float(tr.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1])


def _get_funding_rate(session: HTTP, symbol: str) -> float:
    """Получить последний реализованный funding rate (за 8ч)."""
    resp = session.get_funding_rate_history(
        category="linear", symbol=symbol, limit=1
    )
    if resp["retCode"] != 0:
        raise RuntimeError(f"Funding rate {symbol}: {resp['retMsg']}")
    lst = resp["result"]["list"]
    return float(lst[0]["fundingRate"]) if lst else 0.0


def _get_instrument_info(session: HTTP, symbol: str) -> dict:
    """Получить tick_size, qty_step, min_qty для символа."""
    resp = session.get_instruments_info(category="linear", symbol=symbol)
    if resp["retCode"] != 0:
        raise RuntimeError(f"Инструмент {symbol}: {resp['retMsg']}")
    info = resp["result"]["list"][0]
    return {
        "tick_size": float(info["priceFilter"]["tickSize"]),
        "qty_step":  float(info["lotSizeFilter"]["qtyStep"]),
        "min_qty":   float(info["lotSizeFilter"]["minOrderQty"]),
    }


# ══════════════════════════════════════════════
# Основной класс монитора
# ══════════════════════════════════════════════

class TradeMonitor:
    """
    Мониторинг открытой позиции.

    Подключается к двум WebSocket:
      - Публичный (linear):  ticker → обновление текущей цены
      - Приватный (private): order, position → обнаружение TP-переходов и закрытия

    Жизненный цикл (state machine):
      WATCHING_TP1 → WATCHING_TP2 → RUNNER → CLOSED
              ↘ (SL hit) ↗          ↘ (trailing/SL/funding) ↗
    """

    def __init__(self, trade: dict, session: HTTP) -> None:
        self.trade     = trade
        self.session   = session
        self.symbol    = trade["symbol"]
        self.direction = trade.get("direction") or "long"

        # Сторона ордера, закрывающего позицию (TP / SL / force-close):
        # long закрывается Sell'ом, short закрывается Buy'ом.
        self.exit_side = "Sell" if self.direction == "long" else "Buy"

        # Параметры инструмента
        try:
            instr          = _get_instrument_info(session, self.symbol)
            self.tick_size = instr["tick_size"]
            self.qty_step  = instr["qty_step"]
        except Exception as e:
            log.warning(f"Параметры инструмента недоступны: {e} — defaults")
            self.tick_size = 0.01
            self.qty_step  = 0.001

        # ── Mutable state (всё под lock) ──────────────────────────
        self.lock              = threading.Lock()
        self.phase             = PHASE_WATCHING_TP1
        self.position_qty      = float(trade["qty_total"])
        self.current_price     = float(trade["entry_price"])

        # Цены исполнения TP (уточняются при реальных fill)
        self.tp1_fill_price    = float(trade["tp1"])
        self.tp2_fill_price    = float(trade["tp2"])

        # Мониторинг funding rate: время начала превышения порога
        self.funding_above_since: float | None = None
        # ─────────────────────────────────────────────────────────

        # Флаг остановки основного цикла
        self.stop_event = threading.Event()

        # WebSocket-соединения (хранятся для корректного закрытия)
        self.ws_public:  WebSocket | None = None
        self.ws_private: WebSocket | None = None


    # ══════════════════════════════════════════
    # Инициализация: проверка состояния
    # ══════════════════════════════════════════

    def _check_initial_state(self) -> None:
        """
        Запрашивает текущий размер позиции через HTTP.
        Если монитор запускается повторно после TP1/TP2 — восстанавливает
        правильную фазу и переустанавливает трейлинг-стоп.
        """
        log.info("Проверка актуального состояния позиции...")
        try:
            resp = self.session.get_positions(
                category="linear",
                symbol=self.symbol,
                settleCoin="USDT",
            )
            if resp["retCode"] != 0:
                log.warning(f"get_positions: {resp['retMsg']}")
                return

            for pos in resp["result"]["list"]:
                if pos["symbol"] != self.symbol:
                    continue

                actual_qty = float(pos.get("size", 0))
                if actual_qty == 0:
                    log.warning("Позиция уже закрыта (size=0) — нечего мониторить")
                    with self.lock:
                        self.phase = PHASE_CLOSED
                    return

                with self.lock:
                    self.position_qty = actual_qty

                # Ожидаемые остатки после каждого TP
                t          = self.trade
                after_tp1  = t["qty_total"] - t["qty_tp1"]
                after_tp2  = t["qty_total"] - t["qty_tp1"] - t["qty_tp2"]
                tol        = self.qty_step * 2  # допуск на округление

                if actual_qty <= after_tp2 + tol:
                    log.info(f"Позиция {actual_qty} → восстановление фазы RUNNER")
                    self._enter_runner_phase(restored=True)

                elif actual_qty <= after_tp1 + tol:
                    log.info(f"Позиция {actual_qty} → восстановление фазы WATCHING_TP2")
                    self._activate_trailing_tp1(restored=True)

                else:
                    log.info(f"Позиция {actual_qty} → фаза WATCHING_TP1 (без изменений)")

                return

        except Exception as e:
            log.error(f"Ошибка проверки начального состояния: {e}")


    # ══════════════════════════════════════════
    # WebSocket callbacks
    # ══════════════════════════════════════════

    def on_ticker(self, msg: dict) -> None:
        """Публичный WS — обновление текущей цены из тикера."""
        data = msg.get("data", {})
        price_str = data.get("lastPrice")
        if price_str:
            with self.lock:
                self.current_price = float(price_str)


    def on_order_update(self, msg: dict) -> None:
        """
        Приватный WS — обновления ордеров.
        Первичный метод обнаружения исполнения TP1 и TP2:
        сравниваем цену исполнения с ценами TP (допуск 0.5%).
        """
        for order in msg.get("data", []):
            if order.get("symbol") != self.symbol:
                continue
            if order.get("side") != self.exit_side:
                continue
            if order.get("orderStatus") not in ("Filled", "PartiallyFilled"):
                continue

            avg_price = float(order.get("avgPrice") or 0)
            if avg_price == 0:
                continue

            with self.lock:
                phase = self.phase
                tp1   = float(self.trade["tp1"])
                tp2   = float(self.trade["tp2"])

            qty_filled = float(order.get("cumExecQty") or 0)

            # TP1 сработал
            if phase == PHASE_WATCHING_TP1 and abs(avg_price - tp1) / tp1 < 0.005:
                log.info(f"[ORDER] TP1 исполнен @ {avg_price:.4f}  qty={qty_filled}")
                with self.lock:
                    self.tp1_fill_price = avg_price
                self._activate_trailing_tp1()

            # TP2 сработал
            elif phase == PHASE_WATCHING_TP2 and abs(avg_price - tp2) / tp2 < 0.005:
                log.info(f"[ORDER] TP2 исполнен @ {avg_price:.4f}  qty={qty_filled}")
                with self.lock:
                    self.tp2_fill_price = avg_price
                self._enter_runner_phase()


    def on_position_update(self, msg: dict) -> None:
        """
        Приватный WS — обновления позиции.
        Резервный метод обнаружения TP-переходов и полного закрытия.
        Срабатывает если order callback не поймал событие (например, при реконнекте).
        """
        for pos in msg.get("data", []):
            if pos.get("symbol") != self.symbol:
                continue

            new_qty = float(pos.get("size", 0))

            with self.lock:
                old_qty    = self.position_qty
                phase      = self.phase
                cur_price  = self.current_price

            if new_qty == old_qty:
                continue  # нет изменений

            log.info(f"[POSITION] {old_qty:.4g} → {new_qty:.4g}")

            with self.lock:
                self.position_qty = new_qty

            # Полное закрытие позиции
            if new_qty == 0:
                log.info("Позиция закрыта (size=0) — обнаружено через position stream")
                self._handle_position_closed(cur_price, phase)
                return

            # Резервная проверка TP-переходов
            t         = self.trade
            after_tp1 = t["qty_total"] - t["qty_tp1"]
            after_tp2 = t["qty_total"] - t["qty_tp1"] - t["qty_tp2"]
            tol       = self.qty_step * 2

            if phase == PHASE_WATCHING_TP1 and new_qty <= after_tp1 + tol:
                log.info("TP1 обнаружен через position stream (резерв)")
                self._activate_trailing_tp1()

            elif phase == PHASE_WATCHING_TP2 and new_qty <= after_tp2 + tol:
                log.info("TP2 обнаружен через position stream (резерв)")
                self._enter_runner_phase()


    # ══════════════════════════════════════════
    # Переходы state machine
    # ══════════════════════════════════════════

    def _activate_trailing_tp1(self, restored: bool = False) -> None:
        """
        WATCHING_TP1 → WATCHING_TP2.

        После взятия TP1 (+2R):
        - убираем жёсткий стоп-лосс (уже в зоне прибыли)
        - ставим трейлинг 15% на всю оставшуюся позицию (TP2 qty + runner)

        15% — широкий трейлинг, даём позиции "дышать" до TP2.
        """
        with self.lock:
            if self.phase == PHASE_WATCHING_TP2 and not restored:
                return  # уже переключились (дубликат события)
            self.phase    = PHASE_WATCHING_TP2
            cur_price = self.current_price

        if not restored:
            log.info("─" * 55)
            log.info(f"  TP1 ДОСТИГНУТ!  @ ${cur_price:.4f}")
            log.info(f"  Активация трейлинг-стоп 15%")
            log.info("─" * 55)

        trailing_pct  = 0.15
        trailing_dist = _round_price(cur_price * trailing_pct, self.tick_size)

        try:
            self.session.set_trading_stop(
                category="linear",
                symbol=self.symbol,
                stopLoss="0",                    # снять жёсткий SL
                trailingStop=_fmt(trailing_dist),
                positionIdx=0,
            )
            log.info(
                f"Трейлинг 15% установлен: "
                f"${cur_price:.4f} × 15% = ${trailing_dist:.4f}"
            )
        except Exception as e:
            log.error(f"Ошибка установки трейлинга (TP1 → TP2): {e}")


    def _enter_runner_phase(self, restored: bool = False) -> None:
        """
        WATCHING_TP2 → RUNNER.

        После взятия TP2 (+4R):
        - пересчитываем ATR прямо сейчас (свежие данные)
        - сужаем трейлинг до max(2.5·ATR/цена, 10%)
        - runner (40% исходного qty) работает только с этим трейлингом
        """
        with self.lock:
            if self.phase == PHASE_RUNNER and not restored:
                return
            self.phase    = PHASE_RUNNER
            cur_price = self.current_price

        if not restored:
            log.info("─" * 55)
            log.info(f"  TP2 ДОСТИГНУТ!  @ ${cur_price:.4f}")
            log.info(f"  Сужение трейлинга до ATR-значения...")
            log.info("─" * 55)

        # Пересчёт ATR в момент перехода в runner-фазу
        trailing_pct = 0.10  # fallback если API недоступен
        try:
            df_1h        = _get_klines(self.session, self.symbol, "60", limit=50)
            atr_fresh    = _calculate_atr(df_1h, period=14)
            trailing_pct = max(2.5 * atr_fresh / cur_price, 0.10)
            log.info(
                f"ATR(14,1H) = {atr_fresh:.4f}  "
                f"→  трейлинг = {trailing_pct:.2%}"
            )
        except Exception as e:
            log.warning(f"ATR для runner недоступен: {e} — используем 10%")

        trailing_dist = _round_price(cur_price * trailing_pct, self.tick_size)

        try:
            self.session.set_trading_stop(
                category="linear",
                symbol=self.symbol,
                trailingStop=_fmt(trailing_dist),
                positionIdx=0,
            )
            log.info(
                f"Трейлинг обновлён: {trailing_pct:.2%} "
                f"(${cur_price:.4f} × {trailing_pct:.2%} = ${trailing_dist:.4f})"
            )
        except Exception as e:
            log.error(f"Ошибка обновления трейлинга (runner): {e}")


    def _handle_position_closed(self, exit_price: float, phase: str) -> None:
        """
        Финальный обработчик: позиция полностью закрыта.

        Определяет тип выхода, рассчитывает итоговый P&L с учётом
        всех частичных закрытий (TP1, TP2, runner/SL), записывает в БД.
        Устанавливает stop_event для остановки основного цикла.
        """
        with self.lock:
            if self.phase == PHASE_CLOSED:
                return  # уже обработали (двойное событие)
            self.phase = PHASE_CLOSED

        # Уточнить цену последнего исполнения через историю
        exit_price_actual = exit_price
        try:
            resp = self.session.get_executions(
                category="linear",
                symbol=self.symbol,
                limit=10,
            )
            if resp["retCode"] == 0:
                for ex in resp["result"]["list"]:
                    # Ищем последнее закрывающее исполнение (Sell для long, Buy для short)
                    if ex.get("side") == self.exit_side:
                        exit_price_actual = float(ex["execPrice"])
                        break
        except Exception:
            pass  # используем цену из WebSocket-тикера

        # Тип выхода: если закрылись до TP1 — это стоп-лосс
        if phase == PHASE_WATCHING_TP1:
            exit_type = "stop_loss"
        else:
            exit_type = "trailing_stop"

        # Расчёт совокупного P&L
        # Для short знак движения цены инвертирован: прибыль растёт при падении цены.
        trade = self.trade
        entry = float(trade["entry_price"])
        sign  = 1.0 if self.direction == "long" else -1.0
        pnl   = 0.0

        with self.lock:
            tp1_px = self.tp1_fill_price
            tp2_px = self.tp2_fill_price

        # Вклад TP1 (30%) — если фаза прошла мимо WATCHING_TP1
        if phase in (PHASE_WATCHING_TP2, PHASE_RUNNER):
            leg = sign * (tp1_px - entry) * float(trade["qty_tp1"])
            pnl += leg
            log.info(f"  TP1: ({tp1_px:.4f} - {entry:.4f}) × {trade['qty_tp1']} × {sign:+.0f} = ${leg:+.2f}")

        # Вклад TP2 (30%) — если дошли до runner-фазы
        if phase == PHASE_RUNNER:
            leg = sign * (tp2_px - entry) * float(trade["qty_tp2"])
            pnl += leg
            log.info(f"  TP2: ({tp2_px:.4f} - {entry:.4f}) × {trade['qty_tp2']} × {sign:+.0f} = ${leg:+.2f}")

        # Вклад финального закрытия
        if phase == PHASE_WATCHING_TP1:
            # Стоп сработал до TP1 — закрылся весь объём
            close_qty = float(trade["qty_total"])
        else:
            # Закрылся runner (40%)
            close_qty = float(trade["qty_runner"])

        final_leg = sign * (exit_price_actual - entry) * close_qty
        pnl += final_leg
        log.info(
            f"  Финал: ({exit_price_actual:.4f} - {entry:.4f}) × {close_qty} × {sign:+.0f} "
            f"= ${final_leg:+.2f}"
        )

        result_r = pnl / float(trade["risk_amount"]) if float(trade["risk_amount"]) > 0 else 0.0

        log.info("═" * 55)
        log.info("  ПОЗИЦИЯ ЗАКРЫТА")
        log.info(f"  Символ:      {self.symbol}")
        log.info(f"  Тип выхода:  {exit_type}")
        log.info(f"  Цена выхода: ${exit_price_actual:.4f}")
        log.info(f"  P&L:         ${pnl:+.2f}")
        log.info(f"  Результат:   {result_r:+.3f}R")
        log.info("═" * 55)

        update_trade(trade["id"], {
            "status":     "closed",
            "exit_price": exit_price_actual,
            "exit_type":  exit_type,
            "result_usd": round(pnl, 2),
            "result_r":   round(result_r, 3),
        })
        log.info(f"Сделка #{trade['id']} обновлена в БД.")

        self.stop_event.set()


    # ══════════════════════════════════════════
    # Фоновые потоки
    # ══════════════════════════════════════════

    def _funding_rate_loop(self) -> None:
        """
        Фоновый поток: проверяет funding rate каждый час.

        Алгоритм:
          - Если abs(funding) > 0.05%: начинаем/продолжаем отсчёт времени.
          - Если превышение длится ≥ 48ч: принудительно закрыть позицию.
          - Если ставка вернулась в норму: сбросить счётчик.

        Это защита от накопленных расходов при очень высоком funding.
        """
        log.info("[FUNDING] Поток мониторинга funding rate запущен")

        while not self.stop_event.is_set():
            try:
                fr     = _get_funding_rate(self.session, self.symbol)
                fr_abs = abs(fr)
                now    = time.time()

                with self.lock:
                    above_since = self.funding_above_since
                    phase       = self.phase

                if phase == PHASE_CLOSED:
                    break

                if fr_abs > FUNDING_THRESHOLD:
                    if above_since is None:
                        with self.lock:
                            self.funding_above_since = now
                        log.warning(
                            f"[FUNDING] {fr_abs:.4%} > порога {FUNDING_THRESHOLD:.4%} "
                            f"— начало отсчёта"
                        )
                    else:
                        hours_elapsed = (now - above_since) / 3600
                        log.warning(
                            f"[FUNDING] {fr_abs:.4%} высокий "
                            f"{hours_elapsed:.1f}ч / {FUNDING_MAX_HOURS}ч"
                        )
                        if hours_elapsed >= FUNDING_MAX_HOURS:
                            log.warning(
                                f"[FUNDING] Порог {FUNDING_MAX_HOURS}ч превышен! "
                                f"Принудительное закрытие."
                            )
                            self._force_close("funding_close")
                            break
                else:
                    if above_since is not None:
                        with self.lock:
                            hours_elapsed = (now - above_since) / 3600
                        log.info(
                            f"[FUNDING] Нормализовался ({fr_abs:.4%}) "
                            f"после {hours_elapsed:.1f}ч. Счётчик сброшен."
                        )
                    with self.lock:
                        self.funding_above_since = None

            except Exception as e:
                log.error(f"[FUNDING] Ошибка: {e}")

            # Проверяем каждый час (wait с timeout позволяет быстро выйти по stop_event)
            self.stop_event.wait(timeout=3600)


    def _status_loop(self) -> None:
        """
        Фоновый поток: вывод статуса каждые 10 минут.
        Показывает фазу, текущую цену, unrealized PnL, дистанцию в R.
        """
        while not self.stop_event.is_set():
            with self.lock:
                phase   = self.phase
                qty     = self.position_qty
                price   = self.current_price
                above   = self.funding_above_since

            if phase == PHASE_CLOSED:
                break

            entry = float(self.trade["entry_price"])
            stop  = float(self.trade["stop_price"])
            sign  = 1.0 if self.direction == "long" else -1.0
            risk  = abs(entry - stop)

            unrealized = sign * (price - entry) * qty
            r_current  = sign * (price - entry) / risk if risk > 0 else 0

            funding_note = ""
            if above is not None:
                h = (time.time() - above) / 3600
                funding_note = f"  ⚠ funding высокий {h:.1f}ч"

            log.info(
                f"[{phase.upper()}] "
                f"цена=${price:.4f}  "
                f"qty={qty:.4g}  "
                f"unrealized=${unrealized:+.2f}  "
                f"R={r_current:+.2f}"
                f"{funding_note}"
            )

            self.stop_event.wait(timeout=600)


    def _force_close(self, exit_type: str) -> None:
        """
        Принудительно закрыть оставшуюся позицию рыночным IOC-ордером.
        Используется при превышении порога funding rate.
        Снимает все стопы перед рыночным закрытием.
        """
        with self.lock:
            qty       = self.position_qty
            cur_price = self.current_price
            phase     = self.phase

        if qty <= 0 or phase == PHASE_CLOSED:
            log.info("[FORCE CLOSE] Позиция уже закрыта или пуста.")
            return

        log.info(f"[FORCE CLOSE] Принудительное закрытие {qty:.4g} ({exit_type})")

        # Снять трейлинг и SL перед рыночным ордером во избежание двойного закрытия
        try:
            self.session.set_trading_stop(
                category="linear",
                symbol=self.symbol,
                trailingStop="0",
                stopLoss="0",
                positionIdx=0,
            )
        except Exception as e:
            log.warning(f"[FORCE CLOSE] Сброс стопов: {e}")

        # Рыночный ордер (reduce-only, IOC гарантирует немедленное исполнение)
        try:
            resp = self.session.place_order(
                category="linear",
                symbol=self.symbol,
                side=self.exit_side,
                orderType="Market",
                qty=_fmt(qty),
                reduceOnly=True,
                timeInForce="IOC",
            )
            if resp["retCode"] != 0:
                log.error(f"[FORCE CLOSE] Ошибка рыночного ордера: {resp['retMsg']}")
                return
            log.info("[FORCE CLOSE] Рыночный ордер на закрытие отправлен")
        except Exception as e:
            log.error(f"[FORCE CLOSE] Критическая ошибка: {e}")
            return

        # Ждём несколько секунд — позиция должна обновиться через WebSocket
        time.sleep(5)

        # Если WebSocket не поймал закрытие — вызвать вручную
        with self.lock:
            still_open = (self.phase != PHASE_CLOSED)

        if still_open:
            self._handle_position_closed(cur_price, phase)
            # Перезаписать тип выхода (он мог быть установлен как trailing_stop)
            update_trade(self.trade["id"], {"exit_type": exit_type})


    # ══════════════════════════════════════════
    # Основной метод запуска
    # ══════════════════════════════════════════

    def run(self) -> None:
        """
        Запустить полный цикл мониторинга:
        1. Проверить состояние позиции через HTTP (на случай рестарта)
        2. Подключить публичный WS (тикер)
        3. Подключить приватный WS (ордера, позиции)
        4. Запустить фоновые потоки (funding, статус)
        5. Ждать stop_event
        """
        log.info("═" * 55)
        log.info(f"  POSITION MONITOR ЗАПУЩЕН")
        log.info(f"  Символ: {self.symbol}  ({self.direction.upper()})")
        log.info(f"  Trade ID: {self.trade['id']}")
        log.info(f"  Вход: ${self.trade['entry_price']:.4f}")
        log.info(f"  SL:   ${self.trade['stop_price']:.4f}")
        log.info(f"  TP1:  ${self.trade['tp1']:.4f}  qty={self.trade['qty_tp1']}")
        log.info(f"  TP2:  ${self.trade['tp2']:.4f}  qty={self.trade['qty_tp2']}")
        log.info(f"  Runner:                 qty={self.trade['qty_runner']:.4g}")
        log.info(f"  Трейлинг (после TP2): {float(self.trade['trailing_pct']):.2%}")
        log.info("═" * 55)

        # Восстановление фазы если монитор перезапустился
        self._check_initial_state()

        with self.lock:
            if self.phase == PHASE_CLOSED:
                log.info("Позиция уже закрыта. Выход.")
                return

        # Публичный WebSocket: тикер → текущая цена
        self.ws_public = WebSocket(testnet=TESTNET, channel_type="linear")
        self.ws_public.ticker_stream(symbol=self.symbol, callback=self.on_ticker)

        # Приватный WebSocket: ордера + позиции
        self.ws_private = WebSocket(
            testnet=TESTNET,
            api_key=API_KEY,
            api_secret=API_SECRET,
            channel_type="private",
        )
        self.ws_private.order_stream(callback=self.on_order_update)
        self.ws_private.position_stream(callback=self.on_position_update)

        log.info("WebSocket подключены. Ожидание событий...")

        # Фоновые потоки (daemon — завершатся вместе с основным процессом)
        threading.Thread(
            target=self._funding_rate_loop,
            name="funding-checker",
            daemon=True,
        ).start()

        threading.Thread(
            target=self._status_loop,
            name="status-logger",
            daemon=True,
        ).start()

        # Главный цикл: блокируемся до stop_event
        try:
            while not self.stop_event.is_set():
                self.stop_event.wait(timeout=30)

        except KeyboardInterrupt:
            log.info("Остановка по Ctrl+C")

        finally:
            if self.ws_public:
                self.ws_public.exit()
            if self.ws_private:
                self.ws_private.exit()
            log.info("WebSocket закрыты.")

        with self.lock:
            final_phase = self.phase

        if final_phase == PHASE_CLOSED:
            log.info("Мониторинг завершён: позиция закрыта.")
        else:
            log.info(
                f"Мониторинг остановлен вручную. "
                f"Позиция ещё открыта (фаза: {final_phase})."
            )


# ══════════════════════════════════════════════
# Точка входа
# ══════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Position Monitor — Bull Market Breakout System"
    )
    parser.add_argument(
        "trade_id",
        type=int,
        nargs="?",
        help="ID сделки в БД (если не указан — последняя открытая сделка)",
    )
    args = parser.parse_args()

    session = _create_session()

    # Загрузить сделку из БД
    if args.trade_id:
        trade = get_trade_by_id(args.trade_id)
        if not trade:
            log.error(f"Сделка #{args.trade_id} не найдена в базе данных")
            sys.exit(1)
    else:
        trade = get_open_trade()
        if not trade:
            log.error("В базе данных нет открытых сделок. Запустите trade_entry.py.")
            sys.exit(1)

    if trade["status"] != "open":
        log.error(
            f"Сделка #{trade['id']} имеет статус '{trade['status']}' "
            f"(не 'open'). Выход."
        )
        sys.exit(1)

    log.info(
        f"Сделка загружена: #{trade['id']} | "
        f"{trade['symbol']} | "
        f"entry=${trade['entry_price']} | "
        f"qty={trade['qty_total']}"
    )

    monitor = TradeMonitor(trade, session)
    monitor.run()


if __name__ == "__main__":
    main()
