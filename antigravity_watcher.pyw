#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
antigravity_watcher.pyw
Фоновый монитор процессов Antigravity (IDE/Tools) и Google Gemini Desktop.
Автоматически поднимает antigravity_manager_proxy.py в фоне при запуске приложений,
включает системный прокси для Gemini и останавливает мост после закрытия программ.
"""

from __future__ import annotations

import atexit
import os
import platform
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Set

# Имена процессов Antigravity
ANTIGRAVITY_PROCESSES: Set[str] = {
    "antigravity tools.exe",
    "antigravity-tools.exe",
    "antigravity manager.exe",
    "antigravity-manager.exe",
    "antigravity.exe",
    "antigravity ide.exe",
    "agy.exe",
    "language_server.exe",
    "language_server_windows_x64.exe",
    "language_server_windows",
    "antigravity-tools",
    "antigravity-manager",
    "antigravity",
}

# Имена процессов Gemini Desktop
GEMINI_PROCESSES: Set[str] = {
    "gemini.exe",
}

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 18888
CHECK_INTERVAL_SEC = 2.0

system_proxy_active = False
_mutex_handle = None


def acquire_single_instance_mutex(name: str = "Global\\AntigravityWatcherMutex") -> Optional[object]:
    """Гарантирует, что запущен только один экземпляр watcher."""
    if platform.system() == "Windows":
        try:
            import ctypes

            mutex = ctypes.windll.kernel32.CreateMutexW(None, True, name)
            if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                return None
            return mutex
        except Exception:
            return True
    return True


def set_windows_system_proxy(
    enable: bool, host: str = BRIDGE_HOST, port: int = BRIDGE_PORT
) -> bool:
    """
    Включает или выключает системный прокси Windows (WinINet).
    Необходимо для Gemini Desktop (Electron), так как оно не принимает
    --proxy-server и не считывает переменную HTTP_PROXY в Windows.
    """
    if platform.system() != "Windows":
        return False
    try:
        import ctypes
        import winreg

        reg_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, reg_path, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enable:
                winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, f"{host}:{port}")
                winreg.SetValueEx(
                    key, "ProxyOverride", 0, winreg.REG_SZ, "<local>;localhost;127.0.0.1"
                )
            else:
                winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)

        # Оповещаем сетевую подсистему Windows
        ctypes.windll.wininet.InternetSetOptionW(0, 39, 0, 0)  # INTERNET_OPTION_SETTINGS_CHANGED
        ctypes.windll.wininet.InternetSetOptionW(0, 37, 0, 0)  # INTERNET_OPTION_REFRESH
        return True
    except Exception:
        return False


def is_port_listening(host: str, port: int) -> bool:
    """Проверяет, запущен ли уже локальный прокси-мост."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def get_running_process_names() -> Set[str]:
    """Возвращает список запущенных процессов в нижнем регистре."""
    names: Set[str] = set()
    system = platform.system()
    try:
        if system == "Windows":
            creationflags = 0x08000000  # CREATE_NO_WINDOW
            out = subprocess.check_output(
                ["tasklist", "/NH", "/FO", "CSV"],
                creationflags=creationflags,
                stderr=subprocess.DEVNULL,
                text=True,
                errors="ignore",
            )
            for line in out.splitlines():
                if line.startswith('"'):
                    proc = line.split('"', 2)[1].lower()
                    names.add(proc)
        else:
            out = subprocess.check_output(
                ["ps", "-A", "-o", "comm="],
                stderr=subprocess.DEVNULL,
                text=True,
                errors="ignore",
            )
            for line in out.splitlines():
                proc = os.path.basename(line.strip()).lower()
                names.add(proc)
    except Exception:
        pass
    return names


def start_proxy_script_in_background(script_path: Path) -> Optional[subprocess.Popen]:
    """Запускает antigravity_manager_proxy.py в полностью скрытом фоновом режиме."""
    system = platform.system()
    cmd = [
        sys.executable,
        str(script_path),
        "--bridge-only",
        "--patch-accounts",
    ]

    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "cwd": str(script_path.parent),
    }

    if system == "Windows":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True

    try:
        return subprocess.Popen(cmd, **kwargs)
    except Exception:
        return None


def cleanup() -> None:
    global system_proxy_active, _mutex_handle
    if system_proxy_active:
        set_windows_system_proxy(False)
        system_proxy_active = False
    if _mutex_handle and platform.system() == "Windows":
        try:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(_mutex_handle)
            _mutex_handle = None
        except Exception:
            pass


atexit.register(cleanup)


def main() -> int:
    global system_proxy_active, _mutex_handle

    _mutex_handle = acquire_single_instance_mutex()
    if not _mutex_handle:
        # Экземпляр watcher уже запущен в фоне
        return 0

    base_dir = Path(__file__).resolve().parent
    proxy_script = base_dir / "antigravity_manager_proxy.py"

    if not proxy_script.exists():
        return 1

    # Завершаем при сигналах SIGINT / SIGTERM
    def signal_handler(signum, frame):
        cleanup()
        sys.exit(0)

    try:
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
    except Exception:
        pass

    bridge_proc: Optional[subprocess.Popen] = None

    # При первом запуске проверяем доступность моста
    running_names = get_running_process_names()
    antigravity_running = bool(running_names.intersection(ANTIGRAVITY_PROCESSES))
    gemini_running = bool(running_names.intersection(GEMINI_PROCESSES))

    if (antigravity_running or gemini_running) and not is_port_listening(BRIDGE_HOST, BRIDGE_PORT):
        bridge_proc = start_proxy_script_in_background(proxy_script)
        time.sleep(1.5)

    if gemini_running:
        if set_windows_system_proxy(True):
            system_proxy_active = True

    while True:
        try:
            running_names = get_running_process_names()
            antigravity_running = bool(running_names.intersection(ANTIGRAVITY_PROCESSES))
            gemini_running = bool(running_names.intersection(GEMINI_PROCESSES))
            any_app_running = antigravity_running or gemini_running

            if any_app_running:
                # Если приложение запущено, а мост упал или не стартовал — поднимаем
                if bridge_proc is None or bridge_proc.poll() is not None:
                    if not is_port_listening(BRIDGE_HOST, BRIDGE_PORT):
                        bridge_proc = start_proxy_script_in_background(proxy_script)

                # Для Gemini Desktop активируем системный прокси
                if gemini_running and not system_proxy_active:
                    if set_windows_system_proxy(True):
                        system_proxy_active = True
                elif not gemini_running and system_proxy_active:
                    set_windows_system_proxy(False)
                    system_proxy_active = False

            else:
                # Все приложения закрыты — выключаем системный прокси и мост
                if system_proxy_active:
                    set_windows_system_proxy(False)
                    system_proxy_active = False

                if bridge_proc is not None and bridge_proc.poll() is None:
                    bridge_proc.terminate()
                    try:
                        bridge_proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        bridge_proc.kill()
                    bridge_proc = None

            time.sleep(CHECK_INTERVAL_SEC)
        except KeyboardInterrupt:
            cleanup()
            if bridge_proc is not None and bridge_proc.poll() is None:
                bridge_proc.terminate()
            break
        except Exception:
            time.sleep(CHECK_INTERVAL_SEC)

    cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
