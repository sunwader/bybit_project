"""
app.py — веб-журнал сделок.
Breakout / Breakdown System (long и short).

Flask + SQLite + тёмный HTML/CSS фронтенд.

Запуск:
    python app.py
    Открыть: http://localhost:5000
"""

import os
import math
from flask import Flask, render_template, request, jsonify, redirect, url_for

from dotenv import load_dotenv
from pybit.unified_trading import HTTP

from db import init_db, get_all_trades, get_trade_by_id, update_trade

# ──────────────────────────────────────────────
# Конфигурация
# ──────────────────────────────────────────────

load_dotenv()

API_KEY    = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")
TESTNET    = os.getenv("TESTNET", "false").lower() == "true"
PORT       = int(os.getenv("FLASK_PORT", "5000"))

# ──────────────────────────────────────────────
# Flask приложение
# ──────────────────────────────────────────────

app = Flask(__name__)

# Инициализировать Bybit-сессию (публичная, без auth — для получения цен)
try:
    _bybit = HTTP(testnet=TESTNET)
except Exception:
    _bybit = None

# Создать таблицу БД при старте если ещё не существует
init_db()


# ══════════════════════════════════════════════
# Фильтры Jinja2 — форматирование значений
# ══════════════════════════════════════════════

@app.template_filter("usd")
def fmt_usd(v) -> str:
    """Число → строка '$1,234.56' или '—'."""
    if v is None:
        return "—"
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


@app.template_filter("pct")
def fmt_pct(v) -> str:
    """Доля (0.085) → '8.5%' или '—'."""
    if v is None:
        return "—"
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


@app.template_filter("r_fmt")
def fmt_r(v) -> str:
    """R-результат → '+1.23R' или '—'."""
    if v is None:
        return "—"
    try:
        return f"{float(v):+.2f}R"
    except (TypeError, ValueError):
        return "—"


@app.template_filter("sign_class")
def sign_class(v) -> str:
    """Определить CSS-класс 'green'/'red' по знаку значения."""
    try:
        return "green" if float(v) > 0 else ("red" if float(v) < 0 else "")
    except (TypeError, ValueError):
        return ""


# ══════════════════════════════════════════════
# Статистика
# ══════════════════════════════════════════════

def calculate_stats(trades: list[dict]) -> dict:
    """
    Рассчитать статистику по всем сделкам:
    win rate, avg R, profit factor, max drawdown, exit type breakdown.

    Учитываются только закрытые сделки (status='closed') с result_r != None.
    """
    closed = [
        t for t in trades
        if t.get("status") == "closed"
        and t.get("result_r") is not None
        and t.get("result_usd") is not None
    ]
    open_count = sum(1 for t in trades if t.get("status") == "open")

    empty = {
        "total_closed":   0,
        "open_count":     open_count,
        "wins":           0,
        "losses":         0,
        "win_rate":       0.0,
        "avg_r":          0.0,
        "profit_factor":  0.0,
        "profit_factor_str": "—",
        "max_drawdown":   0.0,
        "total_pnl":      0.0,
        "exit_types":     {},
    }
    if not closed:
        return empty

    wins   = [t for t in closed if float(t["result_r"]) > 0]
    losses = [t for t in closed if float(t["result_r"]) <= 0]

    win_rate = len(wins) / len(closed) * 100
    avg_r    = sum(float(t["result_r"]) for t in closed) / len(closed)

    gross_profit = sum(float(t["result_usd"]) for t in wins)
    gross_loss   = abs(sum(float(t["result_usd"]) for t in losses))

    if gross_loss > 0:
        profit_factor     = gross_profit / gross_loss
        profit_factor_str = f"{profit_factor:.2f}"
    elif gross_profit > 0:
        profit_factor     = float("inf")
        profit_factor_str = "∞"
    else:
        profit_factor     = 0.0
        profit_factor_str = "0.00"

    # Max drawdown: максимальное падение от пика кумулятивного P&L
    sorted_closed = sorted(
        closed,
        key=lambda x: (x.get("date") or "", x.get("time") or ""),
    )
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for t in sorted_closed:
        equity += float(t["result_usd"])
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    total_pnl = sum(float(t["result_usd"]) for t in closed)

    # Количество сделок по типу выхода
    exit_types: dict[str, int] = {}
    for t in closed:
        et = t.get("exit_type") or "unknown"
        exit_types[et] = exit_types.get(et, 0) + 1

    return {
        "total_closed":      len(closed),
        "open_count":        open_count,
        "wins":              len(wins),
        "losses":            len(losses),
        "win_rate":          win_rate,
        "avg_r":             avg_r,
        "profit_factor":     profit_factor if not math.isinf(profit_factor) else 999,
        "profit_factor_str": profit_factor_str,
        "max_drawdown":      max_dd,
        "total_pnl":         total_pnl,
        "exit_types":        exit_types,
    }


# ══════════════════════════════════════════════
# Маршруты
# ══════════════════════════════════════════════

@app.route("/")
def index():
    """
    Главная страница: список всех сделок + блок статистики.
    """
    trades = get_all_trades()
    stats  = calculate_stats(trades)
    return render_template("index.html", trades=trades, stats=stats)


@app.route("/trade/<int:trade_id>")
def trade_detail(trade_id: int):
    """
    Детальная страница сделки с полными параметрами и полем заметок.
    """
    trade = get_trade_by_id(trade_id)
    if not trade:
        return "Сделка не найдена", 404
    return render_template("trade.html", trade=trade)


@app.route("/trade/<int:trade_id>/notes", methods=["POST"])
def save_notes(trade_id: int):
    """
    Сохранить заметку к сделке (POST из формы на странице детали).
    Перенаправляет обратно на страницу сделки.
    """
    notes = request.form.get("notes", "").strip()
    update_trade(trade_id, {"notes": notes})
    return redirect(url_for("trade_detail", trade_id=trade_id))


@app.route("/api/live")
def api_live():
    """
    JSON-эндпоинт для автоматического обновления открытых сделок.
    Возвращает текущую цену и unrealized PnL для каждой открытой сделки.
    Опрашивается JavaScript каждые 30 секунд.
    """
    if _bybit is None:
        return jsonify([])

    open_trades = [t for t in get_all_trades() if t.get("status") == "open"]
    if not open_trades:
        return jsonify([])

    # Группируем по символу чтобы делать один запрос на символ
    by_symbol: dict[str, list[dict]] = {}
    for t in open_trades:
        sym = t["symbol"]
        by_symbol.setdefault(sym, []).append(t)

    result = []
    for sym, sym_trades in by_symbol.items():
        try:
            resp = _bybit.get_tickers(category="linear", symbol=sym)
            if resp["retCode"] != 0:
                continue
            price = float(resp["result"]["list"][0]["lastPrice"])

            for t in sym_trades:
                entry      = float(t["entry_price"])
                qty        = float(t["qty_total"])
                sign       = 1.0 if (t.get("direction") or "long") == "long" else -1.0
                unrealized = sign * (price - entry) * qty
                result.append({
                    "id":         t["id"],
                    "symbol":     sym,
                    "price":      price,
                    "unrealized": unrealized,
                })
        except Exception:
            continue

    return jsonify(result)


# ══════════════════════════════════════════════
# Запуск
# ══════════════════════════════════════════════

if __name__ == "__main__":
    # threaded=True нужен для /api/live при параллельных запросах
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
