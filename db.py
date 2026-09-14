"""
db.py — модуль работы с базой данных SQLite.
Инициализация схемы, сохранение и обновление записей сделок.
"""

import os
import sqlite3
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# Путь к файлу базы данных берётся из .env или используется default
DB_PATH = Path(os.getenv("DB_PATH", "trades.db"))


def get_connection() -> sqlite3.Connection:
    """Открыть соединение с SQLite, вернуть объекты через Row-фабрику."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """
    Создать таблицу trades если она ещё не существует.
    Вызывается при каждом запуске — безопасно (IF NOT EXISTS).
    """
    conn = get_connection()
    try:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,

            -- Дата и время входа
            date                TEXT,
            time                TEXT,

            -- Основные параметры сделки
            symbol              TEXT NOT NULL,
            direction           TEXT DEFAULT 'long',   -- 'long' или 'short'
            entry_price         REAL,
            breakout_level      REAL,
            atr                 REAL,

            -- Стоп
            stop_price          REAL,
            stop_distance_pct   REAL,

            -- Позиция и маржа
            position_notional   REAL,
            leverage            INTEGER,
            required_margin     REAL,

            -- Риск
            risk_amount         REAL,
            risk_pct            REAL,

            -- Тейк-профиты
            tp1                 REAL,
            tp2                 REAL,
            trailing_pct        REAL,

            -- Объёмы
            qty_total           REAL,
            qty_tp1             REAL,
            qty_tp2             REAL,
            qty_runner          REAL,

            -- Фильтры входа
            btc_regime          TEXT,
            volume_24h          REAL,
            open_interest       REAL,
            spread              REAL,
            funding_rate        REAL,

            -- Ордера
            entry_order_id      TEXT,

            -- Статус и результат
            status              TEXT DEFAULT 'open',
            exit_price          REAL,
            exit_type           TEXT,
            result_usd          REAL,
            result_r            REAL,

            -- Заметки (для веб-журнала)
            notes               TEXT,

            -- Временны́е метки
            created_at          TEXT,
            updated_at          TEXT
        )
        """)
        conn.commit()

        # Миграция для БД, созданных до появления шорт-стратегии
        try:
            conn.execute("ALTER TABLE trades ADD COLUMN direction TEXT DEFAULT 'long'")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # колонка уже существует
    finally:
        conn.close()


def save_trade(trade_data: dict) -> int:
    """
    Сохранить новую сделку в базу данных.
    Возвращает id созданной записи.
    """
    conn = get_connection()
    try:
        columns = ", ".join(trade_data.keys())
        placeholders = ", ".join(["?" for _ in trade_data])
        values = list(trade_data.values())

        cursor = conn.execute(
            f"INSERT INTO trades ({columns}) VALUES ({placeholders})",
            values,
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def update_trade(trade_id: int, updates: dict) -> None:
    """
    Обновить поля существующей сделки по id.
    Автоматически проставляет updated_at = текущее время.
    """
    conn = get_connection()
    try:
        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
        values = list(updates.values()) + [trade_id]

        conn.execute(
            f"UPDATE trades SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
            values,
        )
        conn.commit()
    finally:
        conn.close()


def get_open_trade(symbol: Optional[str] = None) -> Optional[dict]:
    """
    Получить последнюю открытую сделку из БД.
    Если задан symbol — фильтровать по нему.
    """
    conn = get_connection()
    try:
        if symbol:
            row = conn.execute(
                "SELECT * FROM trades WHERE status = 'open' AND symbol = ? ORDER BY id DESC LIMIT 1",
                (symbol,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM trades WHERE status = 'open' ORDER BY id DESC LIMIT 1"
            ).fetchone()

        return dict(row) if row else None
    finally:
        conn.close()


def get_trade_by_id(trade_id: int) -> Optional[dict]:
    """Получить сделку по id."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_all_trades() -> list[dict]:
    """Получить все сделки (для веб-журнала), отсортированные по дате."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
