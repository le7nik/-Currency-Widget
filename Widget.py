import tkinter as tk
import requests
import threading
import logging
import logging.handlers
import os
import sys
import queue
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
BG_COLOR = "#1e1e1e"
FG_COLOR = "#ffffff"
UP_COLOR = "#4caf50"
DOWN_COLOR = "#f44336"
NEUTRAL_COLOR = "#b0b0b0"
DELTA_UP_COLOR = "#81c784"      # светлее зелёного
DELTA_DOWN_COLOR = "#e57373"    # светлее красного
HTTP_TIMEOUT = 5

ALLOWED_HOSTS = {"www.cbr-xml-daily.ru", "api.coingecko.com"}

# =====================================================================
#  ЛОГИРОВАНИЕ (в папке проекта)
# =====================================================================
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent   # Для .exe: папка, где лежит сам .exe
else:
    BASE_DIR = Path(__file__).resolve().parent  # Для .py: папка со скриптом
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
    "USD": None, "EUR": None, "JPY": None, "BTC": None, "ETH": None
}
price_labels: Dict[str, tk.Label] = {}
delta_labels: Dict[str, tk.Label] = {}

session = requests.Session()
session.headers.update({"User-Agent": "CurrencyWidget/1.0"})


def _verify_final_host(response: requests.Response, expected: str) -> bool:
    final_host = urlparse(response.url).hostname or ""
    if final_host != expected:
        log.critical("ПОДОЗРИТЕЛЬНЫЙ РЕДИРЕКТ: ожидался %s, итоговый URL — %s",
                     expected, response.url)
        return False
    return True


def _safe_get(url: str, expected_host: str) -> Optional[Dict[str, Any]]:
    if expected_host not in ALLOWED_HOSTS:
        log.error("ИБ: Запрос к неразрешенному домену — %s", expected_host)
        return None
    log.info("→ GET %s", url)
    t0 = datetime.now()
    try:
        r = session.get(url, timeout=HTTP_TIMEOUT, verify=True, allow_redirects=True)
    except requests.exceptions.Timeout:
        log.error("СЕТЬ: таймаут %ss при запросе к %s", HTTP_TIMEOUT, expected_host)
        return None
    except requests.exceptions.SSLError as e:
        log.error("СЕТЬ: ошибка SSL/TLS для %s — %s", expected_host, e)
        return None
    except requests.exceptions.ConnectionError as e:
        log.error("СЕТЬ: не удалось подключиться к %s — %s", expected_host, e)
        return None
    except requests.exceptions.RequestException as e:
        log.error("СЕТЬ: ошибка запроса к %s — %s: %s", expected_host, type(e).__name__, e)
        return None

    elapsed_ms = (datetime.now() - t0).total_seconds() * 1000
    if not _verify_final_host(r, expected_host):
        return None
    if not r.ok:
        log.warning("ДОСТУП: HTTP %s от %s (%.0f мс)", r.status_code, expected_host, elapsed_ms)
        if r.status_code == 429:
            log.warning("ДОСТУП: сработал rate-limit (429) на %s", expected_host)
        return None
    log.info("← %s %s (%.0f мс, %d байт)", r.status_code, expected_host, elapsed_ms, len(r.content))
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
        log.warning("ОТВЕТ: в JSON ЦБ отсутствует ключ 'Valute'")
        return None
    result = {}
    for code in ("USD", "EUR", "JPY"):
        info = val.get(code)
        if not isinstance(info, dict) or "Value" not in info:
            log.warning("ОТВЕТ: нет данных по %s", code)
            continue
        nominal = info.get("Nominal", 1) or 1
        try:
            result[code] = float(info["Value"]) / nominal
        except (TypeError, ZeroDivisionError) as e:
            log.warning("ОТВЕТ: некорректные данные по %s — %s", code, e)
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
            if not isinstance(price, (int, float)) or price <= 0:
                raise ValueError(f"некорректная цена: {price!r}")
            result[code] = float(price)
        except (KeyError, TypeError, ValueError) as e:
            log.warning("ОТВЕТ: не удалось извлечь цену %s — %s", code, e)
    return result or None

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
    """Возвращает строку вида '+0.25 ₽ (+0.27%)' или '−0.40 ₽ (−0.40%)'."""
    diff = new - old
    if diff == 0:
        return ""
    pct = (diff / old) * 100 if old else 0
    sign = "+" if diff > 0 else "−"  # минус типографский
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
            label = price_labels.get(key)
            delta_label = delta_labels.get(key)
            if label is None:
                log.error("UI: нет метки для %s", key)
                continue

            old_price = previous_prices.get(key)
            is_crypto = key in ("BTC", "ETH")
            new_str = format_price(new_price, is_crypto=is_crypto)

            if old_price is None:
                label.config(text=f"текущая: {new_str}", fg=NEUTRAL_COLOR)
                if delta_label is not None:
                    delta_label.config(text="")
                log.info("ЦЕНА %s: первичное значение %s", key, new_str)
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
                if delta_label is not None:
                    delta_label.config(
                        text=format_delta(old_price, new_price, is_crypto=is_crypto),
                        fg=delta_color,
                    )

                pct = (new_price - old_price) / old_price * 100 if old_price else 0
                log.debug("ЦЕНА %s: %s → %s (%+.2f%%)", key, old_str, new_str, pct)

            previous_prices[key] = new_price
        except Exception:
            log.exception("UI: ошибка обновления строки %s", key)

    if current_data:
        time_label.config(text=f"Обновлено: {datetime.now():%H:%M:%S}")


# =====================================================================
#  ФОНОВЫЙ ВОРКЕР (один поток на всё время жизни)
# =====================================================================
ui_queue: "queue.Queue[Dict[str, float]]" = queue.Queue()
stop_event = threading.Event()


def worker_loop() -> None:
    """Единственный фоновый поток: бесконечный цикл с Event.wait()."""
    log.info("WORKER: поток запущен")
    while not stop_event.is_set():
        try:
            current_data: Dict[str, float] = {}
            fiat = fetch_fiat_rates()
            if fiat:
                current_data.update(fiat)
            crypto = fetch_crypto_prices()
            if crypto:
                current_data.update(crypto)

            if current_data:
                ui_queue.put(current_data)
            else:
                log.warning("ЦИКЛ: не получено ни одной цены (сеть/API)")
        except Exception:
            log.exception("WORKER: сбой цикла обновления")

        # Ждём интервал, но просыпаемся сразу при stop_event.set()
        stop_event.wait(REFRESH_INTERVAL)
    log.info("WORKER: поток остановлен")


def poll_ui_queue(root: tk.Tk, time_label: tk.Label) -> None:
    """ТОЛЬКО главный поток: забирает данные из очереди и обновляет UI."""
    try:
        while True:
            current_data = ui_queue.get_nowait()
            refresh_ui(current_data, time_label)
    except queue.Empty:
        pass
    # Планируем следующий опрос через 100 мс, если приложение живо
    if not stop_event.is_set():
        try:
            root.after(100, lambda: poll_ui_queue(root, time_label))
        except tk.TclError:
            log.info("UI: окно закрыто, polling остановлен")

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

            # Колонка "цена + стрелка"
            price_lbl = tk.Label(row, text="загрузка...", bg=BG_COLOR,
                                 fg=NEUTRAL_COLOR,
                                 font=(FONT_FAMILY, FONT_SIZE_MAIN), anchor="e")
            price_lbl.pack(side="right")

            # Колонка "дельта" (маленькая, под ценой)
            delta_lbl = tk.Label(row, text="", bg=BG_COLOR,
                                 fg=NEUTRAL_COLOR,
                                 font=(FONT_FAMILY, FONT_SIZE_DELTA), anchor="e")
            delta_lbl.pack(side="right", padx=(0, 8))

            price_labels[code] = price_lbl
            delta_labels[code] = delta_lbl

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
    log.info("ПРИЛОЖЕНИЕ: интервал %ss, таймаут %ss", REFRESH_INTERVAL, HTTP_TIMEOUT)

    root = tk.Tk()
    time_label = build_ui(root)

    # Один-единственный поток на всё время работы
    worker = threading.Thread(target=worker_loop, daemon=True, name="net-worker")
    worker.start()

    # Главный поток только опрашивает очередь
    poll_ui_queue(root, time_label)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        log.info("ПРИЛОЖЕНИЕ: прервано пользователем (Ctrl+C)")
    except Exception:
        log.critical("ПРИЛОЖЕНИЕ: аварийное завершение", exc_info=True)
    finally:
        stop_event.set()
        log.info("ПРИЛОЖЕНИЕ: остановка")
