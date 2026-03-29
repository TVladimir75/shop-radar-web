#!/usr/bin/env python3
"""
Запуск сайта. Если порт 8765 занят — пробуем освободить старый процесс, иначе порт 8766…8770.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Доступ в локальной сети: SHOP_RADAR_HOST=0.0.0.0
HOST = (os.environ.get("SHOP_RADAR_HOST") or "127.0.0.1").strip() or "127.0.0.1"
PORTS = list(range(8765, 8771))


def _port_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((HOST, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _kill_listeners_macos(port: int) -> None:
    if sys.platform != "darwin":
        return
    try:
        out = subprocess.run(
            ["lsof", "-ti", f"TCP:{port}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in out.stdout.splitlines():
            pid = line.strip()
            if pid.isdigit():
                try:
                    os.kill(int(pid), 9)
                    print(f"  Остановлен процесс {pid} (занимал порт {port})", flush=True)
                except ProcessLookupError:
                    pass
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass


def _pick_port() -> int:
    first = PORTS[0]
    if _port_free(first):
        return first
    print(f"\n  Порт {first} занят — освобождаю…", flush=True)
    _kill_listeners_macos(first)
    time.sleep(0.8)
    if _port_free(first):
        return first
    for p in PORTS[1:]:
        if _port_free(p):
            print(f"  Использую порт {p} (откройте ссылку ниже)", flush=True)
            return p
    print(
        f"\nНе удалось занять порты {PORTS[0]}–{PORTS[-1]}.\n"
        "Закройте другие программы или перезагрузите Mac.\n",
        flush=True,
    )
    sys.exit(1)


def _guess_lan_ip() -> str | None:
    """Ориентировочный IP в LAN (для подсказки при host=0.0.0.0)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.4)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        return str(ip)
    except OSError:
        return None
    finally:
        s.close()


def _open_browser(url: str) -> None:
    time.sleep(1.8)
    if sys.platform == "darwin":
        os.system(f'open "{url}" 2>/dev/null')
    else:
        webbrowser.open(url)


def main() -> None:
    port = _pick_port()
    local_url = f"http://127.0.0.1:{port}/"
    threading.Thread(target=_open_browser, args=(local_url,), daemon=True).start()
    import uvicorn

    print(f"\n  Локально: {local_url}", flush=True)
    if HOST in ("0.0.0.0", "::"):
        lan = _guess_lan_ip()
        if lan:
            print(f"  В сети Wi‑Fi/LAN: http://{lan}:{port}/", flush=True)
        else:
            print(
                "  Слушаю все интерфейсы (0.0.0.0) — узнайте IP в Системных настройках → Сеть.",
                flush=True,
            )
    print("  Оставьте окно открытым. Стоп: Ctrl+C\n", flush=True)
    uvicorn.run(
        "app.main:app",
        host=HOST,
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
