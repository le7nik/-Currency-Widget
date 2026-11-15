import tkinter as tk
import requests
import threading
import logging
import logging.handlers
import os
import sys
import queue
import colorsys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional, Dict, Any

# --- Конфигурация ---
REFRESH_INTERVAL = 60
FONT_FAMILY = "Segoe UI"
FONT_SIZE_MAIN = 12
FONT_SIZE_HEADER = 10
FONT_SIZE_DELTA = 10

# Цвета
BG_COLOR = "#1e1e1e"
FG_COLOR = "#ffffff"
UP_COLOR = "#4caf50"
DOWN_COLOR = "#f44336"
NEUTRAL_COLOR = "#b0b0b0"
DELTA_UP_COLOR = "#81c784"
DELTA_DOWN_COLOR = "#e57373"
WARN_COLOR = "#ffb300"  # Оранжевый для нейтрального индекса страха

HTTP_TIMEOUT = 5
ALLOWED_HOSTS = {
    "www.cbr-xml-daily.ru",
    "api.coingecko.com",
    "api.alternative.me",
    "pro-api.coinmarketcap.com",
    "open-api-v4.coinglass.com"
}

# Если есть ключ CoinGlass, вставь его сюда (иначе оставь пустым)
COINGLASS_API_KEY = ""

# =====================================================================
#  ЛОГИРОВАНИЕ (в папке проекта)
# =====================================================================
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent  # Для .exe
else:
    BASE_DIR = Path(__file__).resolve().parent  # Для .py

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "widget.log"


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("currency_widget")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s:%(lineno)d — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        fh = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError as e:
        print(f"Не удалось открыть лог-файл {LOG_FILE}: {e}", file=sys.stderr)
    if sys.stderr is not None:
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)
        logger.addHandler(ch)
    return logger


log = setup_logging()


def log_uncaught(exc_type, exc_value, exc_tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    log.critical("НЕОБРАБОТАННОЕ ИСКЛЮЧЕНИЕ", exc_info=(exc_type, exc_value, exc_tb))


sys.excepthook = log_uncaught

# =====================================================================
#  СЕТЬ
# =====================================================================
previous_prices: Dict[str, Optional[float]] = {
    "USD": None, "EUR": None, "JPY": None, "BTC": None, "ETH": None, "FG": None
}
price_labels: Dict[str, Any] = {}
delta_labels: Dict[str, tk.Label] = {}

session = requests.Session()
session.headers.update({"User-Agent": "CurrencyWidget/1.1"})


def _verify_final_host(response: requests.Response, expected: str) -> bool:
    final_host = urlparse(response.url).hostname or ""
    if final_host != expected:
        log.critical("ПОДОЗРИТЕЛЬНЫЙ РЕДИРЕКТ: ожидался %s, итоговый URL — %s",
                     expected, response.url)
        return False
    return True


def _safe_get(url: str, expected_host: str, headers: dict = None) -> Optional[Dict[str, Any]]:
    if expected_host not in ALLOWED_HOSTS:
        log.error("ИБ: Запрос к неразрешенному домену — %s", expected_host)
        return None
    try:
        r = session.get(url, timeout=HTTP_TIMEOUT, verify=True, allow_redirects=True, headers=headers)
    except requests.exceptions.RequestException as e:
        log.error("СЕТЬ: ошибка запроса к %s — %s", expected_host, e)
        return None

    if not _verify_final_host(r, expected_host):
        return None
    if not r.ok:
        log.warning("ДОСТУП: HTTP %s от %s", r.status_code, expected_host)
        return None
    try:
        return r.json()
    except ValueError:
        log.warning("ДОСТУП: невалидный JSON от %s", expected_host)
        return None


def fetch_fiat_rates() -> Optional[Dict[str, float]]:
    data = _safe_get("https://www.cbr-xml-daily.ru/daily_json.js", "www.cbr-xml-daily.ru")
    if not data:
        return None
    val = data.get("Valute")
    if not isinstance(val, dict):
        return None
    result = {}
    for code in ("USD", "EUR", "JPY"):
        info = val.get(code)
        if isinstance(info, dict) and "Value" in info:
            nominal = info.get("Nominal", 1) or 1
            result[code] = float(info["Value"]) / nominal
    return result or None


def fetch_crypto_prices() -> Optional[Dict[str, float]]:
    data = _safe_get(
        "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd",
        "api.coingecko.com",
    )
    if not data:
        return None
    result = {}
    for code, cid in (("BTC", "bitcoin"), ("ETH", "ethereum")):
        try:
            price = data[cid]["usd"]
            if isinstance(price, (int, float)) and price > 0:
                result[code] = float(price)
        except (KeyError, TypeError):
            pass
    return result or None


def fetch_fear_greed_index() -> Optional[float]:
    """Собирает данные с разных источников и считает среднее."""
    values = []

    # 1. Alternative.me
    try:
        data = _safe_get("https://api.alternative.me/fng/?limit=1&format=json", "api.alternative.me")
        if data and 'data' in data and len(data['data']) > 0:
            values.append(int(data['data'][0]['value']))
    except Exception as e:
        log.debug("FG: Ошибка Alternative.me — %s", e)

    # 2. CoinMarketCap (Keyless endpoint)
    try:
        headers = {"Accept": "application/json"}
        data = _safe_get("https://pro-api.coinmarketcap.com/public-api/v3/fear-and-greed/latest",
                         "pro-api.coinmarketcap.com", headers)
        if data and 'data' in data and 'value' in data['data']:
            values.append(int(data['data']['value']))
    except Exception as e:
        log.debug("FG: Ошибка CoinMarketCap — %s", e)

    # 3. CoinGlass (Требует ключ)
    if COINGLASS_API_KEY:
        try:
            headers = {"CG-API-KEY": COINGLASS_API_KEY}
            data = _safe_get("https://open-api-v4.coinglass.com/api/index/fear-greed-history",
                             "open-api-v4.coinglass.com", headers)
            if data and 'data' in data and len(data['data']) > 0:
                # Извлекаем последнее значение (зависит от структуры API)
                val = data['data'][-1][1]
                values.append(int(val))
        except Exception as e:
            log.debug("FG: Ошибка CoinGlass — %s", e)

    if not values:
        return None

    return round(sum(values) / len(values), 1)


# =====================================================================
#  ФОРМАТИРОВАНИЕ
# =====================================================================
def format_price(value: Optional[float], is_crypto: bool = False) -> str:
    if value is None:
        return "---"
    if is_crypto:
        return f"${value:,.2f}"
    return f"{value:.2f} ₽"


def format_delta(old: float, new: float, is_crypto: bool = False) -> str:
    diff = new - old
    if diff == 0:
        return ""
    pct = (diff / old) * 100 if old else 0
    sign = "+" if diff > 0 else "−"
    if is_crypto:
        return f"{sign}${abs(diff):,.2f} ({sign}{abs(pct):.2f}%)"
    return f"{sign}{abs(diff):.2f} ₽ ({sign}{abs(pct):.2f}%)"


# =====================================================================
#  UI
# =====================================================================
def refresh_ui(current_data: Dict[str, float], time_label: tk.Label) -> None:
    for key, new_price in current_data.items():
        try:
            if new_price is None:
                continue

            # --- Обработка Индекса Страха и Жадности ---
            if key == "FG":
                label_val = price_labels.get("FG_VAL")
                canvas = price_labels.get("FG_CANVAS")
                marker = price_labels.get("FG_MARKER")
                width = price_labels.get("FG_WIDTH", 320)

                if label_val:
                    if new_price < 25:
                        color = DOWN_COLOR
                    elif new_price > 75:
                        color = UP_COLOR
                    else:
                        color = WARN_COLOR
                    label_val.config(text=f"{new_price:.0f}", fg=color)

                if canvas and marker:
                    x_pos = int((new_price / 100.0) * width)
                    x_pos = max(4, min(x_pos, width - 4))
                    canvas.coords(marker, x_pos - 3, 10, x_pos + 3, 10, x_pos, 16)
                continue
            # -------------------------------------------

            label = price_labels.get(key)
            delta_label = delta_labels.get(key)
            if label is None:
                continue

            old_price = previous_prices.get(key)
            is_crypto = key in ("BTC", "ETH")
            new_str = format_price(new_price, is_crypto=is_crypto)

            if old_price is None:
                label.config(text=f"текущая: {new_str}", fg=NEUTRAL_COLOR)
                if delta_label:
                    delta_label.config(text="")
            else:
                old_str = format_price(old_price, is_crypto=is_crypto)
                if new_price > old_price:
                    arrow, color = "↑", UP_COLOR
                    delta_color = DELTA_UP_COLOR
                elif new_price < old_price:
                    arrow, color = "↓", DOWN_COLOR
                    delta_color = DELTA_DOWN_COLOR
                else:
                    arrow, color = "→", NEUTRAL_COLOR
                    delta_color = NEUTRAL_COLOR

                label.config(text=f"{old_str}  {arrow}  {new_str}", fg=color)
                if delta_label:
                    delta_label.config(
                        text=format_delta(old_price, new_price, is_crypto=is_crypto),
                        fg=delta_color,
                    )

            previous_prices[key] = new_price
        except Exception:
            log.exception("UI: ошибка обновления строки %s", key)

    if current_data:
        time_label.config(text=f"Обновлено: {datetime.now():%H:%M:%S}")


# =====================================================================
#  ФОНОВЫЙ ВОРКЕР
# =====================================================================
ui_queue: "queue.Queue[Dict[str, float]]" = queue.Queue()
stop_event = threading.Event()


def worker_loop() -> None:
    log.info("WORKER: поток запущен")
    while not stop_event.is_set():
        try:
            current_data: Dict[str, float] = {}
            fiat = fetch_fiat_rates()
            if fiat: current_data.update(fiat)

            crypto = fetch_crypto_prices()
            if crypto: current_data.update(crypto)

            fg_val = fetch_fear_greed_index()
            if fg_val is not None:
                current_data["FG"] = fg_val

            if current_data:
                ui_queue.put(current_data)
            else:
                log.warning("ЦИКЛ: не получено ни одной цены")
        except Exception:
            log.exception("WORKER: сбой цикла обновления")

        stop_event.wait(REFRESH_INTERVAL)
    log.info("WORKER: поток остановлен")


def poll_ui_queue(root: tk.Tk, time_label: tk.Label) -> None:
    try:
        while True:
            current_data = ui_queue.get_nowait()
            refresh_ui(current_data, time_label)
    except queue.Empty:
        pass

    if not stop_event.is_set():
        try:
            root.after(100, lambda: poll_ui_queue(root, time_label))
        except tk.TclError:
            pass


# =====================================================================
#  ОКНО
# =====================================================================
def make_draggable(window: tk.Tk) -> None:
    def start_move(e):
        window._x, window._y = e.x, e.y

    def do_move(e):
        x = window.winfo_x() + (e.x - window._x)
        y = window.winfo_y() + (e.y - window._y)
        window.geometry(f"+{x}+{y}")

    window.bind("<ButtonPress-1>", start_move)
    window.bind("<B1-Motion>", do_move)


def hue_to_rgb(hue: int) -> str:
    """Конвертирует оттенок (0-120) в HEX-строку."""
    r, g, b = colorsys.hsv_to_rgb(hue / 360.0, 1.0, 1.0)
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


def build_ui(root: tk.Tk) -> tk.Label:
    root.title("Курсы валют")
    root.configure(bg=BG_COLOR)
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.95)

    root.report_callback_exception = (
        lambda exc, val, tb: log.exception("ОШИБКА В TK-КОЛБЭКЕ", exc_info=(exc, val, tb))
    )

    main = tk.Frame(root, bg=BG_COLOR, padx=10, pady=8)
    main.pack(fill="both", expand=True)

    time_label = tk.Label(main, text="Загрузка...", bg=BG_COLOR,
                          fg="#888888", font=(FONT_FAMILY, FONT_SIZE_HEADER))
    time_label.pack(anchor="w", pady=(0, 5))

    sections = [
        ("Фиат", [("USD", "Доллар США"), ("EUR", "Евро"), ("JPY", "Иена")]),
        ("Крипто", [("BTC", "Bitcoin"), ("ETH", "Ethereum")]),
    ]

    for i, (_section, items) in enumerate(sections):
        if i > 0:
            tk.Frame(main, height=1, bg="#555555").pack(fill="x", pady=(8, 6))
        for code, name in items:
            row = tk.Frame(main, bg=BG_COLOR)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=f"{name} ({code}):", bg=BG_COLOR, fg=FG_COLOR,
                     font=(FONT_FAMILY, FONT_SIZE_MAIN), anchor="w",
                     width=14).pack(side="left")

            price_lbl = tk.Label(row, text="загрузка...", bg=BG_COLOR,
                                 fg=NEUTRAL_COLOR, font=(FONT_FAMILY, FONT_SIZE_MAIN), anchor="e")
            price_lbl.pack(side="right")

            delta_lbl = tk.Label(row, text="", bg=BG_COLOR,
                                 fg=NEUTRAL_COLOR, font=(FONT_FAMILY, FONT_SIZE_DELTA), anchor="e")
            delta_lbl.pack(side="right", padx=(0, 8))

            price_labels[code] = price_lbl
            delta_labels[code] = delta_lbl

    # --- СЕКЦИЯ ИНДЕКСА СТРАХА И ЖАДНОСТИ ---
    tk.Frame(main, height=1, bg="#555555").pack(fill="x", pady=(8, 6))

    fg_frame = tk.Frame(main, bg=BG_COLOR)
    fg_frame.pack(fill="x", pady=2)

    tk.Label(fg_frame, text="Страх и жадность:", bg=BG_COLOR, fg=NEUTRAL_COLOR,
             font=(FONT_FAMILY, FONT_SIZE_HEADER), anchor="w").pack(side="left")

    fg_val_label = tk.Label(fg_frame, text="--", bg=BG_COLOR, fg=FG_COLOR,
                            font=(FONT_FAMILY, FONT_SIZE_MAIN, "bold"), width=4, anchor="e")
    fg_val_label.pack(side="right")

    # Полоска (Canvas)
    # Фиксируем ширину 350, чтобы градиент и маркер совпадали
    canvas = tk.Canvas(main, height=16, bg=BG_COLOR, highlightthickness=0, width=350)
    canvas.pack(pady=(2, 0))  # Убрали fill="x"

    width = 350
    for x in range(width):
        hue = int((x / width) * 120)
        canvas.create_line(x, 2, x, 12, fill=hue_to_rgb(hue))

    # Маркер (треугольник)
    marker = canvas.create_polygon(0, 12, 6, 12, 3, 16, fill="#ffffff", outline="")

    price_labels["FG_VAL"] = fg_val_label
    price_labels["FG_CANVAS"] = canvas
    price_labels["FG_MARKER"] = marker
    price_labels["FG_WIDTH"] = width
    # ----------------------------------------

    tk.Frame(main, height=1, bg="#444444").pack(fill="x", pady=5)

    close_btn = tk.Label(main, text="✕ Закрыть", bg=BG_COLOR, fg="#ff6b6b",
                         font=(FONT_FAMILY, 9), cursor="hand2")
    close_btn.pack(anchor="e")
    close_btn.bind("<Button-1>", lambda _: root.destroy())

    root.bind("<Escape>", lambda _: root.destroy())
    root.protocol("WM_DELETE_WINDOW", lambda: (stop_event.set(), root.destroy()))

    make_draggable(root)
    root.update_idletasks()
    sw = root.winfo_screenwidth()
    root.geometry(f"+{sw - 400}+50")
    return time_label


# =====================================================================
#  ТОЧКА ВХОДА
# =====================================================================
if __name__ == "__main__":
    log.info("=" * 60)
    log.info("ПРИЛОЖЕНИЕ: запуск CurrencyWidget")
    log.info("ПРИЛОЖЕНИЕ: лог-файл — %s", LOG_FILE)

    root = tk.Tk()
    time_label = build_ui(root)

    worker = threading.Thread(target=worker_loop, daemon=True, name="net-worker")
    worker.start()

    poll_ui_queue(root, time_label)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        log.info("ПРИЛОЖЕНИЕ: прервано пользователем")
    except Exception:
        log.critical("ПРИЛОЖЕНИЕ: аварийное завершение", exc_info=True)
    finally:
        stop_event.set()
        log.info("ПРИЛОЖЕНИЕ: остановка")
