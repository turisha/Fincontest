import os
import time
import math
import random
import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests
import pandas as pd
import numpy as np


# =========================
# НАСТРОЙКИ (меняешь только тут)
# =========================

# Имя бота/портфеля в ArenaGo (из secrets FINCONTEST_BOT)
BOT_NAME = os.getenv("FINCONTEST_BOT", "fincontest")

# Токен ArenaGo (из secrets FINCONTEST_TOKEN)
TOKEN_ENV_NAME = "FINCONTEST_TOKEN"

# Вселенная тикеров, которыми бот ИМЕННО ТОРГУЕТ
# Важно: тикеры должны быть латиницей (например X5, а не "Х5")
TICKERS = [
    "SBER", "GAZP", "LKOH", "ROSN", "TATN", "NVTK",
    "GMKN", "CHMF", "NLMK", "YDEX", "AFLT", "ALRS", "MGNT"
]

# Таймфрейм свечей (мин): 30 даёт больше активности, чем 60
TIMEFRAME_MIN = 30

# Как часто обновляться (сек). Не надо слишком часто, чтобы не ловить rate limit
POLL_SEC = 60

# История для индикаторов
HISTORY_DAYS = 30

# Параметры стратегии (SMA crossover)
SMA_FAST = 8
SMA_SLOW = 21

# ATR фильтр "не торгуем во флэте"
# 0.003 = 0.3%
ATR_PERIOD = 14
ATR_FILTER_MIN = 0.003

# Риск / экспозиция
MAX_TOTAL_EXPOSURE = 0.70   # доля капитала, которая может быть в рынке
MAX_PER_TICKER = 0.10       # доля капитала на один тикер (если тикеров много)

# Минимальная сумма сделки (руб), чтобы не плодить микросделки и комиссию
MIN_TRADE_VALUE = 15000

# Лимит сделок в день (по правилам турнира)
TRADES_LIMIT_PER_DAY = 200

# Шорты
ALLOW_SHORT = False  # если True — бот будет шортить

# Дневной стоп: если equity упала на X% от начала дня — больше не торгуем до завтра
DAILY_STOP_LOSS = -0.03  # -3% за день

# Стоп по ATR от цены входа (работает как выход в 0, если цена ушла против позиции)
ATR_STOP_MULT = 3.0

# Пульс/heartbeat
HEARTBEAT_SEC = 300  # раз в 5 минут

# Торговые окна турнира (МСК): будни 07:05–18:40 и 19:05–23:45
TRADE_WINDOWS: List[Tuple[dt.time, dt.time]] = [
    (dt.time(7, 5), dt.time(18, 40)),
    (dt.time(19, 5), dt.time(23, 45)),
]

# Telegram уведомления (опционально): создай secrets
# TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
TELEGRAM_ON = True


# =========================
# УТИЛИТЫ ВРЕМЕНИ
# =========================

def now_msk() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)

def in_trading_time() -> bool:
    msk = now_msk()
    if msk.weekday() >= 5:  # 5=сб, 6=вс
        return False
    t = msk.time()
    return any(start <= t <= end for start, end in TRADE_WINDOWS)


# =========================
# НАДЁЖНЫЕ HTTP-ЗАПРОСЫ
# =========================

def http_request(
    method: str,
    url: str,
    *,
    session: Optional[requests.Session] = None,
    timeout: int = 20,
    retries: int = 3,
    backoff_base: float = 0.8,
    backoff_jitter: float = 0.4,
    **kwargs
) -> requests.Response:
    """
    Делаем запрос с ретраями и backoff на сетевые/5xx/429 проблемы.
    """
    s = session or requests.Session()
    last_exc = None
    for attempt in range(retries + 1):
        try:
            resp = s.request(method, url, timeout=timeout, **kwargs)

            # 429/5xx: пробуем повторить
            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt < retries:
                    sleep_s = backoff_base * (2 ** attempt) + random.random() * backoff_jitter
                    time.sleep(sleep_s)
                    continue
            return resp
        except Exception as e:
            last_exc = e
            if attempt < retries:
                sleep_s = backoff_base * (2 ** attempt) + random.random() * backoff_jitter
                time.sleep(sleep_s)
                continue
            raise last_exc

def safe_json(resp: requests.Response, tag: str) -> Dict[str, Any]:
    """
    Пытаемся распарсить JSON. Если пришёл HTML/пусто — печатаем диагностически и кидаем исключение.
    """
    try:
        return resp.json()
    except Exception:
        txt = (resp.text or "")[:250].replace("\n", " ")
        print(f"[HTTP ERR] {tag} status={resp.status_code} text='{txt}'")
        raise


# =========================
# TELEGRAM
# =========================

def notify(text: str):
    if not TELEGRAM_ON:
        return
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    try:
        http_request("POST", url, json=payload, timeout=10, retries=1)
    except Exception:
        pass


# =========================
# ARENAGO API
# =========================

ARENAGO_BASE = "https://arenago.ru"

class Arena:
    def __init__(self, token: str):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": token})

    def bots(self) -> List[Dict[str, Any]]:
        r = http_request("GET", f"{ARENAGO_BASE}/api/bots", session=self.s, timeout=15, retries=3)
        return safe_json(r, "arenago bots")

    def positions(self, bot: str) -> List[Dict[str, Any]]:
        r = http_request("GET", f"{ARENAGO_BASE}/api/positions/{bot}", session=self.s, timeout=15, retries=3)
        return safe_json(r, "arenago positions")

    def order(self, bot: str, secid: str, direction: str, qty: int) -> Dict[str, Any]:
        payload = {"direction": direction, "secid": secid, "quantity": int(qty), "bot": bot}
        r = http_request("POST", f"{ARENAGO_BASE}/api/submit_order", session=self.s, json=payload, timeout=15, retries=2)
        return safe_json(r, "arenago submit_order")


# =========================
# MOEX ISS: свечи
# =========================

MOEX_CANDLES_URL = "https://iss.moex.com/iss/engines/stock/markets/shares/securities/{secid}/candles.json"

def moex_candles(secid: str, interval_min: int, days: int) -> pd.DataFrame:
    till = dt.date.today() + dt.timedelta(days=1)
    fr = dt.date.today() - dt.timedelta(days=days)
    params = {
        "from": fr.isoformat(),
        "till": till.isoformat(),
        "interval": interval_min,
        "iss.meta": "off"
    }
    r = http_request("GET", MOEX_CANDLES_URL.format(secid=secid), params=params, timeout=20, retries=3)
    if r.status_code != 200:
        txt = (r.text or "")[:200].replace("\n", " ")
        print(f"[HTTP ERR] moex candles {secid} status={r.status_code} text='{txt}'")
        return pd.DataFrame()

    j = safe_json(r, f"moex candles {secid}")
    candles = j.get("candles", {})
    cols = candles.get("columns", [])
    rows = candles.get("data", [])
    df = pd.DataFrame(rows, columns=cols)
    if df.empty:
        return df

    df["begin"] = pd.to_datetime(df["begin"])
    for c in ["open", "high", "low", "close", "volume", "value"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=["close"]).sort_values("begin").reset_index(drop=True)
    return df


# =========================
# ИНДИКАТОРЫ
# =========================

def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()

def atr(df: pd.DataFrame, n: int) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1
    ).max(axis=1)
    return tr.rolling(n).mean()

def crossover_signal(df: pd.DataFrame) -> Optional[str]:
    """
    Возвращает:
    - "LONG" если fast пересёк slow снизу вверх
    - "SHORT" если fast пересёк slow сверху вниз
    - None если сигнала нет
    """
    if df is None or df.empty:
        return None
    if len(df) < max(SMA_FAST, SMA_SLOW) + 2:
        return None

    close = df["close"]
    f = sma(close, SMA_FAST)
    sl = sma(close, SMA_SLOW)

    f_prev, f_cur = f.iloc[-2], f.iloc[-1]
    s_prev, s_cur = sl.iloc[-2], sl.iloc[-1]
    if np.isnan([f_prev, f_cur, s_prev, s_cur]).any():
        return None

    if f_prev <= s_prev and f_cur > s_cur:
        return "LONG"
    if f_prev >= s_prev and f_cur < s_cur:
        return "SHORT"
    return None


# =========================
# ПОРТФЕЛЬ
# =========================

@dataclass
class Position:
    qty: int
    avg_price: float

def get_cash(bots_json: List[Dict[str, Any]], name: str) -> Optional[float]:
    b = next((x for x in (bots_json or []) if x.get("name") == name), None)
    if not b:
        return None
    return float(b.get("cash_balance", 0.0))

def get_pos(positions_json: List[Dict[str, Any]], secid: str) -> Position:
    for p in positions_json or []:
        if str(p.get("secid")) == secid:
            return Position(qty=int(p.get("position", 0)), avg_price=float(p.get("average_price", 0.0) or 0.0))
    return Position(qty=0, avg_price=0.0)

def estimate_equity(cash: float, positions_json: List[Dict[str, Any]], last_prices: Dict[str, float]) -> float:
    eq = float(cash)
    for p in positions_json or []:
        secid = str(p.get("secid"))
        qty = int(p.get("position", 0))
        px = float(last_prices.get(secid, 0.0))
        eq += qty * px
    return eq


# =========================
# MAIN
# =========================

def main():
    token = os.getenv(TOKEN_ENV_NAME, "").strip()
    if not token:
        raise SystemExit(f"Нет токена: добавь secret {TOKEN_ENV_NAME} в Codespaces.")

    arena = Arena(token)

    # Счётчик сделок в день (только ордера, которые мы отправили)
    trades_today = 0
    trades_date = dt.date.today()

    # Для дневного стопа
    day_start_equity: Optional[float] = None

    # Heartbeat
    last_heartbeat_ts = 0.0

    # История свечей и last_begin (для ускорения)
    history: Dict[str, pd.DataFrame] = {}
    last_begin: Dict[str, Optional[pd.Timestamp]] = {}

    # первичная загрузка по торговым тикерам
    for t in TICKERS:
        df = moex_candles(t, TIMEFRAME_MIN, HISTORY_DAYS)
        history[t] = df
        last_begin[t] = (df["begin"].iloc[-1] if not df.empty else None)

    print("[INIT] bot started. Trading tickers:", TICKERS)
    notify(f"🤖 Bot started: {BOT_NAME}. Trading {len(TICKERS)} tickers.")

    while True:
        try:
            # Новый день — сбрасываем счётчик
            if dt.date.today() != trades_date:
                trades_date = dt.date.today()
                trades_today = 0
                day_start_equity = None

            if trades_today >= TRADES_LIMIT_PER_DAY:
                time.sleep(POLL_SEC)
                continue

            bots = arena.bots()
            cash = get_cash(bots, BOT_NAME)
            if cash is None:
                print(f"[ERR] Бот/портфель '{BOT_NAME}' не найден на ArenaGo.")
                time.sleep(POLL_SEC)
                continue

            pos = arena.positions(BOT_NAME)

            # Тикеры из позиций — только для расчёта equity/heartbeat
            pos_tickers = [str(p.get("secid")) for p in (pos or [])]
            all_tickers = sorted(set(TICKERS) | set(pos_tickers))

            # Обновляем цены/свечи для all_tickers (чтобы equity было адекватным)
            last_prices: Dict[str, float] = {}
            for t in all_tickers:
                df = moex_candles(t, TIMEFRAME_MIN, 7)
                if df.empty:
                    # если нет данных — пробуем оценить через прошлую историю
                    if t in history and not history[t].empty:
                        last_prices[t] = float(history[t]["close"].iloc[-1])
                    continue

                prev = last_begin.get(t)
                if prev is None or df["begin"].iloc[-1] > prev:
                    history[t] = df
                    last_begin[t] = df["begin"].iloc[-1]
                else:
                    # даже если свеча не новая — обновим историю последним df (на случай корректировок)
                    history[t] = df

                last_prices[t] = float(history[t]["close"].iloc[-1])

            equity = estimate_equity(cash, pos, last_prices)

            # Инициализируем дневной equity-старт
            if day_start_equity is None:
                day_start_equity = equity

            # Дневной стоп: прекращаем торговать (не закрывая принудительно позиции)
            if equity <= day_start_equity * (1 + DAILY_STOP_LOSS):
                if time.time() - last_heartbeat_ts >= HEARTBEAT_SEC:
                    msk = now_msk()
                    print(f"[STOP] {msk:%Y-%m-%d %H:%M:%S} MSK | equity~={equity:.2f} start~={day_start_equity:.2f} -> STOP until tomorrow")
                    notify(f"🛑 STOP: дневная просадка. equity~={equity:.0f}, start~={day_start_equity:.0f}. Торговля остановлена до завтра.")
                    last_heartbeat_ts = time.time()
                time.sleep(POLL_SEC)
                continue

            # Heartbeat
            now_ts = time.time()
            if now_ts - last_heartbeat_ts >= HEARTBEAT_SEC:
                msk = now_msk()
                status = "TRADE" if in_trading_time() else "WAIT"
                pos_short = []
                for p in (pos or []):
                    q = int(p.get("position", 0))
                    if q != 0:
                        pos_short.append(f"{p.get('secid')}:{q}")
                pos_str = ", ".join(pos_short) if pos_short else "none"
                print(f"[HB] {msk:%Y-%m-%d %H:%M:%S} MSK | {status} | trades_today={trades_today} | cash={cash:.2f} | equity~={equity:.2f} | pos={pos_str}")
                last_heartbeat_ts = now_ts

            # Вне торговых окон — не торгуем
            if not in_trading_time():
                time.sleep(POLL_SEC)
                continue

            # ===== Торговая логика: только по TICKERS =====
            # Распределение экспозиции: одинаково на тикер, но не больше MAX_PER_TICKER
            per_ticker_target = min(MAX_PER_TICKER, MAX_TOTAL_EXPOSURE / max(1, len(TICKERS)))

            for t in TICKERS:
                df = history.get(t)
                if df is None or df.empty:
                    continue
                if len(df) < max(SMA_FAST, SMA_SLOW, ATR_PERIOD) + 2:
                    continue

                px = float(df["close"].iloc[-1])
                if px <= 0:
                    continue

                a = atr(df, ATR_PERIOD).iloc[-1]
                if np.isnan(a) or a <= 0:
                    continue

                # фильтр флэта
                if (a / px) < ATR_FILTER_MIN:
                    continue

                sig = crossover_signal(df)
                if sig is None:
                    continue

                curr = get_pos(pos, t)
                curr_qty, avg_price = curr.qty, curr.avg_price

                # целевой размер позиции
                target_abs = int(math.floor((equity * per_ticker_target) / px))
                if target_abs <= 0:
                    continue

                if sig == "LONG":
                    target_qty = target_abs
                else:  # SHORT
                    target_qty = -target_abs if ALLOW_SHORT else 0

                # ATR-stop: если позиция уже есть и цена ушла сильно против — выходим в 0
                if curr_qty != 0 and avg_price > 0:
                    if curr_qty > 0 and px <= avg_price - ATR_STOP_MULT * a:
                        target_qty = 0
                    if curr_qty < 0 and px >= avg_price + ATR_STOP_MULT * a:
                        target_qty = 0

                delta = target_qty - curr_qty
                if delta == 0:
                    continue

                direction = "B" if delta > 0 else "S"
                qty = abs(delta)

                # не торгуем микросуммами
                if qty * px < MIN_TRADE_VALUE:
                    continue

                resp = arena.order(BOT_NAME, t, direction, qty)
                if resp.get("success") is True:
                    trades_today += 1
                    side = "BUY" if direction == "B" else "SELL"
                    price_exec = resp.get("price")
                    comm = resp.get("commission")
                    msg = (f"✅ Сделка {BOT_NAME}\n"
                           f"{t} {side} x{qty}\n"
                           f"Цена: {price_exec}\n"
                           f"Комиссия: {comm}\n"
                           f"Сделок сегодня: {trades_today}")
                    print(f"[TRADE] {t} {side} x{qty} px~{px:.2f} sig={sig} trades_today={trades_today}")
                    notify(msg)

                    if trades_today >= TRADES_LIMIT_PER_DAY:
                        notify(f"⚠️ Достигнут лимит {TRADES_LIMIT_PER_DAY} сделок/день. Останавливаю торговлю до завтра.")
                        break
                else:
                    print(f"[ERR] submit_order failed: {resp}")

            time.sleep(POLL_SEC)

        except KeyboardInterrupt:
            print("Остановлено пользователем.")
            break
        except Exception as e:
            # главное — не падать. Подождём и продолжим.
            print(f"[EXC] {type(e).__name__}: {e}")
            time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()