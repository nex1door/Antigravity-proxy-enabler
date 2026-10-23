#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
antigravity_launcher.pyw
Запуск Google Antigravity IDE через локальный прокси-мост (127.0.0.1:18888).

ПОЧЕМУ ЭТО НУЖНО:
  Antigravity состоит из двух частей, и обе игнорируют системный прокси Windows
  по-своему:
    * Chromium/Electron (Antigravity.exe) — берёт настройки из WinINet ИЛИ из
      аргумента --proxy-server. Переменные HTTP_PROXY он НЕ читает.
    * language_server.exe (Go) — читает ТОЛЬКО переменные окружения
      HTTP_PROXY / HTTPS_PROXY / NO_PROXY (реестр Windows ему неинтересен).

  Поэтому если запускать Antigravity обычным ярлыком (просто Antigravity.exe),
  language_server ходит напрямую с российского IP, и IDE пишет
  "not available in your location".

ЧТО ДЕЛАЕТ ЛАУНЧЕР:
  1) проверяет, слушает ли локальный мост 127.0.0.1:18888, и при необходимости
     поднимает его скрыто (antigravity_manager_proxy.py --bridge-only);
  2) запускает Antigravity.exe с --proxy-server=http://127.0.0.1:18888
     (для Electron) и с переменными HTTP_PROXY/HTTPS_PROXY/NO_PROXY
     (для Go language_server);
  3) ждёт закрытия IDE и, если мост поднимал именно он, гасит его.

Запускать нужно ИМЕННО этот файл (ярлык на рабочем столе и в меню «Пуск»
уже настроены на него). Консольного окна нет, т.к. расширение .pyw.
"""

from __future__ import annotations

import os
import platform
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import antigravity_manager_proxy as mgr  # noqa: E402

BRIDGE_HOST = mgr.LOCAL_BRIDGE_HOST
BRIDGE_PORT = mgr.LOCAL_BRIDGE_PORT
BRIDGE_URL = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}"

MANAGER_SCRIPT = BASE_DIR / "antigravity_manager_proxy.py"
LOG_PATH = BASE_DIR / "antigravity_launcher.log"

CREATE_NO_WINDOW = 0x08000000
BRIDGE_START_TIMEOUT_SEC = 30.0


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}\n"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def is_port_listening(host: str = BRIDGE_HOST, port: int = BRIDGE_PORT) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def find_antigravity_exe() -> Optional[Path]:
    """Ищет установленный Antigravity IDE (Electron-приложение Google)."""
    home = Path.home()
    local_app = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
    candidates: List[Path] = [
        local_app / "Programs" / "antigravity" / "Antigravity.exe",
        local_app / "Programs" / "Antigravity" / "Antigravity.exe",
        local_app / "Antigravity" / "Antigravity.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Antigravity"
        / "Antigravity.exe",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def ensure_bridge() -> Optional[subprocess.Popen]:
    """Поднимает локальный мост, если он ещё не слушает порт."""
    if is_port_listening():
        log(f"мост уже активен на {BRIDGE_URL}")
        return None

    if not MANAGER_SCRIPT.exists():
        log(f"ОШИБКА: не найден {MANAGER_SCRIPT}")
        return None

    log("поднимаю локальный мост (antigravity_manager_proxy.py --bridge-only)")
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "cwd": str(BASE_DIR),
    }
    if platform.system() == "Windows":
        kwargs["creationflags"] = CREATE_NO_WINDOW

    proc = subprocess.Popen(
        [sys.executable, str(MANAGER_SCRIPT), "--bridge-only", "--patch-accounts"],
        **kwargs,
    )

    deadline = time.time() + BRIDGE_START_TIMEOUT_SEC
    while time.time() < deadline:
        if is_port_listening():
            log(f"мост поднялся на {BRIDGE_URL}")
            return proc
        if proc.poll() is not None:
            log("ОШИБКА: процесс моста завершился, не открыв порт")
            return None
        time.sleep(0.5)

    log("ПРЕДУПРЕЖДЕНИЕ: мост не открыл порт за отведённое время")
    return proc


def build_proxy_environment() -> dict:
    """Окружение для Antigravity: его Go language_server читает только эти переменные."""
    env = os.environ.copy()
    no_proxy = "localhost,127.0.0.1,::1,0.0.0.0,.local"
    env.update(
        {
            "HTTP_PROXY": BRIDGE_URL,
            "HTTPS_PROXY": BRIDGE_URL,
            "ALL_PROXY": BRIDGE_URL,
            "http_proxy": BRIDGE_URL,
            "https_proxy": BRIDGE_URL,
            "all_proxy": BRIDGE_URL,
            "GRPC_PROXY_EXP": BRIDGE_URL,
            "NODE_USE_ENV_PROXY": "1",
            "NO_PROXY": no_proxy,
            "no_proxy": no_proxy,
        }
    )
    return env


def main() -> int:
    exe = find_antigravity_exe()
    if not exe:
        log("ОШИБКА: Antigravity.exe не найден")
        return 1

    bridge_proc = ensure_bridge()

    cmd = [
        str(exe),
        f"--proxy-server={BRIDGE_URL}",
        "--proxy-bypass-list=<local>;localhost;127.0.0.1;::1",
    ] + sys.argv[1:]

    log(f"запуск: {' '.join(cmd)}")
    try:
        app = subprocess.Popen(cmd, env=build_proxy_environment(), cwd=str(BASE_DIR))
    except Exception as exc:  # noqa: BLE001
        log(f"ОШИБКА запуска Antigravity: {exc}")
        if bridge_proc is not None:
            bridge_proc.terminate()
        return 1

    try:
        code = app.wait()
    except KeyboardInterrupt:
        code = 0

    log(f"Antigravity закрыт (код {code})")
    if bridge_proc is not None and bridge_proc.poll() is None:
        log("гашу мост, поднятый лаунчером")
        bridge_proc.terminate()
        try:
            bridge_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            bridge_proc.kill()
    return code


if __name__ == "__main__":
    sys.exit(main())