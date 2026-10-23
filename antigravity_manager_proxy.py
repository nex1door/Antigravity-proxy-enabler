#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Antigravity Manager — Full HTTP Proxy Router & Region Bypass Suite
Поддерживает отдельные поля IP, PORT, LOGIN, PASSWORD и формат ip:port:login:pass
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import select
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ==============================================================================
# ВПИШИТЕ ДАННЫЕ ВАШЕГО ПРОКСИ СЮДА (4 ОТДЕЛЬНЫХ ПОЛЯ):
# ==============================================================================
PROXY_IP = ""       # Например: "185.100.20.30"
PROXY_PORT = 0            # Например: 8080
PROXY_LOGIN = ""             # Например: "my_login" (оставьте "", если без пароля)
PROXY_PASSWORD = ""          # Например: "my_password" (оставьте "", если без пароля)

# Либо можно вставить одной строкой в формате "ip:port:login:password"
# (если заполнено, имеет приоритет над полями выше):
RAW_PROXY_STRING = ""        # Например: "185.100.20.30:8080:my_login:my_password"

# Локальный порт, который будет слушать Antigravity Manager (без пароля)
LOCAL_BRIDGE_HOST = "127.0.0.1"
LOCAL_BRIDGE_PORT = 18888
STREAM_SOCKET_TIMEOUT = 180.0

ANTIGRAVITY_ENDPOINTS = [
    ("Antigravity CloudCode SSE", "https://daily-cloudcode-pa.googleapis.com"),
    ("Cloud AI Companion API", "https://cloudaicompanion.googleapis.com"),
    ("Google OAuth2 Token API", "https://oauth2.googleapis.com"),
    ("Antigravity Portal", "https://antigravity.google"),
    ("Google Gemini Web/App", "https://gemini.google.com"),
    ("Google Generative Language API", "https://generativelanguage.googleapis.com"),
]

TARGET_PROCESSES = [
    "Antigravity Tools.exe",
    "antigravity-tools.exe",
    "Antigravity Manager.exe",
    "antigravity-manager.exe",
    "agy.exe",
    "language_server.exe",
    "language_server_windows_x64.exe",
    "language_server_windows",
    "Antigravity.exe",
    "Antigravity IDE.exe",
    "node.exe",
    "Gemini.exe",
    "gemini.exe",
    "GeminiAppLauncher.exe",
    "geminiapplauncher.exe",
]


class UpstreamProxyConfig:
    """
    Универсальный парсер прокси. Понимает:
    1) Отдельные поля (IP, порт, логин, пароль) — безопасно экранирует любые спецсимволы
    2) Формат магазина прокси: ip:port:login:password
    3) Формат: login:password@ip:port или http://login:password@ip:port
    """

    def __init__(
        self,
        raw_input: Optional[str] = None,
        ip: str = PROXY_IP,
        port: int = PROXY_PORT,
        login: str = PROXY_LOGIN,
        password: str = PROXY_PASSWORD,
    ) -> None:
        self.scheme: str = "http"
        self.host: str = ""
        self.port: int = 0
        self.username: Optional[str] = None
        self.password: Optional[str] = None

        text = (raw_input or "").strip()
        if text:
            self._parse_flexible_string(text)
        else:
            self.host = ip.strip()
            self.port = int(port)
            self.username = login.strip() or None
            self.password = password.strip() or None

        if not self.host or not self.port:
            raise ValueError("Не указан IP или порт прокси.")

    def _parse_flexible_string(self, text: str) -> None:
        # Случай 1: формат ip:port:login:password (без http:// и без @)
        if "://" not in text and "@" not in text and text.count(":") >= 3:
            parts = text.split(":", 3)
            self.host = parts[0].strip()
            self.port = int(parts[1].strip())
            self.username = parts[2].strip() or None
            self.password = parts[3].strip() or None
            return

        # Случай 2: стандартный URL http://login:password@ip:port или ip:port
        raw = text if "://" in text else f"http://{text}"
        parsed = urllib.parse.urlparse(raw)
        if parsed.scheme in ("http", "https"):
            self.scheme = parsed.scheme
        if not parsed.hostname or not parsed.port:
            raise ValueError(f"Не удалось распознать адрес прокси: {text}")

        self.host = parsed.hostname
        self.port = int(parsed.port)
        self.username = (
            urllib.parse.unquote(parsed.username) if parsed.username else None
        )
        self.password = (
            urllib.parse.unquote(parsed.password) if parsed.password else None
        )

    @property
    def has_auth(self) -> bool:
        return bool(self.username and self.password is not None)

    @property
    def auth_header(self) -> Optional[str]:
        if not self.has_auth:
            return None
        creds = f"{self.username}:{self.password}".encode("utf-8")
        return f"Basic {base64.b64encode(creds).decode('ascii')}"

    @property
    def full_url(self) -> str:
        if self.has_auth:
            u = urllib.parse.quote(self.username or "", safe="")
            p = urllib.parse.quote(self.password or "", safe="")
            return f"{self.scheme}://{u}:{p}@{self.host}:{self.port}"
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def safe_display_url(self) -> str:
        if self.has_auth:
            return f"{self.scheme}://{self.username}:***@{self.host}:{self.port}"
        return f"{self.scheme}://{self.host}:{self.port}"


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class LocalProxyBridgeHandler(socketserver.BaseRequestHandler):
    """Локальный мост 127.0.0.1 -> Ваш HTTP-прокси с автоматической Basic-авторизацией."""

    upstream: UpstreamProxyConfig

    def handle(self) -> None:
        try:
            self.request.settimeout(20.0)
            initial_data = self._read_until_header_end(self.request)
            if not initial_data:
                return

            header_end = initial_data.find(b"\r\n\r\n")
            if header_end == -1:
                return

            header_bytes = initial_data[:header_end]
            body_remainder = initial_data[header_end + 4 :]
            lines = header_bytes.decode("iso-8859-1", errors="replace").split("\r\n")
            if not lines:
                return

            request_line = lines[0]
            headers = lines[1:]

            with socket.create_connection(
                (self.upstream.host, self.upstream.port), timeout=20.0
            ) as upstream_sock:
                upstream_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                upstream_sock.settimeout(None)
                self.request.settimeout(None)

                filtered = [
                    h
                    for h in headers
                    if not h.lower().startswith("proxy-authorization:")
                ]
                if self.upstream.auth_header:
                    filtered.append(
                        f"Proxy-Authorization: {self.upstream.auth_header}"
                    )

                rebuilt = (
                    "\r\n".join([request_line] + filtered) + "\r\n\r\n"
                ).encode("iso-8859-1") + body_remainder

                upstream_sock.sendall(rebuilt)
                self._bidirectional_tunnel(self.request, upstream_sock)
        except Exception:
            pass

    @staticmethod
    def _read_until_header_end(sock: socket.socket, max_bytes: int = 65536) -> bytes:
        buf = bytearray()
        while b"\r\n\r\n" not in buf and len(buf) < max_bytes:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)
        return bytes(buf)

    @staticmethod
    def _bidirectional_tunnel(
        client_sock: socket.socket, upstream_sock: socket.socket
    ) -> None:
        sockets = [client_sock, upstream_sock]
        while True:
            readable, _, exceptional = select.select(
                sockets, [], sockets, STREAM_SOCKET_TIMEOUT
            )
            if exceptional or not readable:
                break
            for s in readable:
                other = upstream_sock if s is client_sock else client_sock
                try:
                    data = s.recv(65536)
                    if not data:
                        return
                    other.sendall(data)
                except OSError:
                    return


def start_local_bridge(
    upstream: UpstreamProxyConfig,
    host: str = LOCAL_BRIDGE_HOST,
    port: int = LOCAL_BRIDGE_PORT,
) -> Tuple[ThreadedTCPServer, str]:
    class BoundBridgeHandler(LocalProxyBridgeHandler):
        pass

    BoundBridgeHandler.upstream = upstream
    try:
        server = ThreadedTCPServer((host, port), BoundBridgeHandler)
    except OSError:
        server = ThreadedTCPServer((host, 0), BoundBridgeHandler)

    actual_port = server.server_address[1]
    thread = threading.Thread(
        target=server.serve_forever, name="AntigravityManagerBridge", daemon=True
    )
    thread.start()
    return server, f"http://{host}:{actual_port}"


def verify_proxy_and_region(proxy: UpstreamProxyConfig) -> bool:
    print(f"\n[*] Проверка HTTP-прокси: {proxy.safe_display_url}")
    proxy_handler = urllib.request.ProxyHandler(
        {"http": proxy.full_url, "https": proxy.full_url}
    )
    opener = urllib.request.build_opener(
        proxy_handler,
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )

    try:
        req = urllib.request.Request(
            "http://ip-api.com/json/?fields=status,country,countryCode,city,isp,hosting,query",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with opener.open(req, timeout=12) as resp:
            info = json.loads(resp.read().decode("utf-8", errors="replace"))

        if info.get("status") == "success":
            cc = info.get("countryCode", "??")
            print(f"[+] Выходной IP прокси : {info.get('query')}")
            print(f"[+] Регион / Страна    : {info.get('country')} ({cc}) {info.get('city', '')}")
            print(f"[+] Провайдер (ISP)    : {info.get('isp')}")
            if cc in {"RU", "BY", "CN", "HK", "IR", "KP", "CU", "SY"}:
                print(f"[!] ВНИМАНИЕ: Страна прокси ({cc}) не поддерживается Antigravity!")
            if info.get("hosting"):
                print("[!] ЗАМЕТКА: IP определяется как серверный/хостинговый (Datacenter ASN).")
    except Exception as exc:
        print(f"[-] Ошибка подключения к прокси (проверьте IP/порт/логин/пароль): {exc}")
        return False

    print("\n[*] Проверка HTTPS CONNECT до серверов Google Antigravity:")
    all_ok = True
    for name, url in ANTIGRAVITY_ENDPOINTS:
        try:
            req = urllib.request.Request(
                url, method="HEAD", headers={"User-Agent": "AntigravityManager/4.9"}
            )
            with opener.open(req, timeout=10) as resp:
                print(f"  [OK] {name:<28} -> HTTP {resp.getcode()}")
        except urllib.error.HTTPError as http_err:
            print(f"  [OK] {name:<28} -> TLS-туннель активен (HTTP {http_err.code})")
        except Exception as err:
            print(f"  [ОШИБКА] {name:<24} -> {err}")
            all_ok = False
    return all_ok


def patch_antigravity_manager_configs(
    local_bridge_url: str, patch_accounts: bool = False
) -> None:
    home = Path.home()
    tools_dir = home / ".antigravity_tools"
    gui_config_path = tools_dir / "gui_config.json"
    try:
        tools_dir.mkdir(parents=True, exist_ok=True)
        cfg: Dict[str, Any] = {}
        if gui_config_path.exists():
            backup = gui_config_path.with_suffix(".json.bak")
            if not backup.exists():
                shutil.copy2(gui_config_path, backup)
            raw = gui_config_path.read_text(encoding="utf-8", errors="replace").strip()
            if raw:
                cfg = json.loads(raw)

        proxy_sec = cfg.get("proxy")
        if not isinstance(proxy_sec, dict):
            proxy_sec = {}
            cfg["proxy"] = proxy_sec

        proxy_sec["upstream_proxy"] = local_bridge_url
        proxy_sec["enable_upstream_proxy"] = True
        cfg["upstream_proxy"] = local_bridge_url

        gui_config_path.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"[+] Обновлен конфиг Antigravity-Manager: {gui_config_path}")
    except Exception as exc:
        print(f"[!] Не удалось обновить {gui_config_path}: {exc}")

    if patch_accounts:
        accounts_dir = tools_dir / "accounts"
        if accounts_dir.exists():
            for acc_file in accounts_dir.glob("*.json"):
                try:
                    acc_data = json.loads(acc_file.read_text(encoding="utf-8"))
                    if isinstance(acc_data, dict):
                        acc_data["proxy"] = local_bridge_url
                        acc_data["proxy_url"] = local_bridge_url
                        acc_file.write_text(
                            json.dumps(acc_data, indent=2, ensure_ascii=False),
                            encoding="utf-8",
                        )
                except Exception:
                    pass


class WindowsSystemProxyManager:
    """
    Управление системным прокси Windows (WinINet).
    Необходимо для Gemini Desktop, так как приложение на базе Electron/Chromium
    блокирует аргумент --proxy-server и игнорирует HTTP_PROXY в Windows.
    """

    INTERNET_OPTION_SETTINGS_CHANGED = 39
    INTERNET_OPTION_REFRESH = 37

    @staticmethod
    def is_supported() -> bool:
        return platform.system() == "Windows"

    @classmethod
    def set_proxy(
        cls,
        enable: bool,
        host: str = LOCAL_BRIDGE_HOST,
        port: int = LOCAL_BRIDGE_PORT,
    ) -> bool:
        if not cls.is_supported():
            return False
        try:
            import winreg
            import ctypes

            reg_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, reg_path, 0, winreg.KEY_SET_VALUE
            ) as key:
                if enable:
                    proxy_str = f"{host}:{port}"
                    winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
                    winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, proxy_str)
                    winreg.SetValueEx(
                        key, "ProxyOverride", 0, winreg.REG_SZ, "<local>;localhost;127.0.0.1"
                    )
                else:
                    winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)

            # Оповещаем WinINet для немедленного применения всеми процессами
            ctypes.windll.wininet.InternetSetOptionW(
                0, cls.INTERNET_OPTION_SETTINGS_CHANGED, 0, 0
            )
            ctypes.windll.wininet.InternetSetOptionW(0, cls.INTERNET_OPTION_REFRESH, 0, 0)
            return True
        except Exception as exc:
            print(f"[!] Ошибка переключения системного прокси Windows: {exc}")
            return False


def find_gemini_executable(custom_path: Optional[str] = None) -> Optional[Path]:
    if custom_path:
        p = Path(custom_path).expanduser()
        if p.exists():
            return p

    system = platform.system()
    home = Path.home()
    if system == "Windows":
        local_app = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
        prog_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        prog_files_x86 = Path(
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        )

        root_exe = local_app / "Google" / "Gemini" / "Gemini.exe"
        if root_exe.exists():
            return root_exe

        gemini_dir = local_app / "Google" / "Gemini"
        if gemini_dir.exists():
            app_exes = sorted(gemini_dir.glob("app-*/Gemini.exe"), reverse=True)
            if app_exes:
                return app_exes[0]

        candidates = [
            prog_files / "Google" / "Gemini" / "Gemini.exe",
            prog_files_x86 / "Google" / "Gemini" / "Gemini.exe",
            local_app / "Programs" / "Google" / "Gemini" / "Gemini.exe",
        ]
        for c in candidates:
            if c.exists():
                return c
    return None


def find_manager_executable(custom_path: Optional[str] = None) -> Optional[Path]:
    if custom_path:
        p = Path(custom_path).expanduser()
        if p.exists():
            return p

    system = platform.system()
    home = Path.home()
    candidates: List[Path] = []
    if system == "Windows":
        local_app = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
        prog_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        candidates = [
            local_app / "Programs" / "antigravity" / "Antigravity.exe",
            local_app / "Programs" / "Antigravity" / "Antigravity.exe",
            local_app / "Antigravity" / "Antigravity.exe",
            prog_files / "Antigravity" / "Antigravity.exe",
            local_app / "Programs" / "Antigravity Tools" / "Antigravity Tools.exe",
            local_app / "Antigravity Tools" / "Antigravity Tools.exe",
            prog_files / "Antigravity Tools" / "Antigravity Tools.exe",
            local_app / "Programs" / "Antigravity Manager" / "Antigravity Manager.exe",
            prog_files / "Antigravity Manager" / "Antigravity Manager.exe",
            local_app / "Programs" / "Antigravity" / "Antigravity.exe",
        ]
    elif system == "Darwin":
        candidates = [
            Path("/Applications/Antigravity Tools.app/Contents/MacOS/Antigravity Tools"),
            Path("/Applications/Antigravity Manager.app/Contents/MacOS/Antigravity Manager"),
        ]
    else:
        candidates = [
            Path("/usr/bin/antigravity-tools"),
            Path("/usr/local/bin/antigravity-tools"),
            Path("/usr/bin/antigravity-manager"),
            home / ".local" / "bin" / "antigravity-tools",
        ]

    for c in candidates:
        if c.exists():
            return c
    for name in ("antigravity-tools", "antigravity-manager", "antigravity"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def build_proxy_environment(local_bridge_url: str) -> Dict[str, str]:
    env = os.environ.copy()
    no_proxy = "localhost,127.0.0.1,::1,0.0.0.0,.local"
    env.update(
        {
            "HTTP_PROXY": local_bridge_url,
            "HTTPS_PROXY": local_bridge_url,
            "ALL_PROXY": local_bridge_url,
            "http_proxy": local_bridge_url,
            "https_proxy": local_bridge_url,
            "all_proxy": local_bridge_url,
            "GRPC_PROXY_EXP": local_bridge_url,
            "NODE_USE_ENV_PROXY": "1",
            "NO_PROXY": no_proxy,
            "no_proxy": no_proxy,
        }
    )
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description="Antigravity Manager & Gemini Proxy Router")
    parser.add_argument(
        "-p",
        "--proxy",
        help="Прокси в формате ip:port:login:pass или http://login:pass@ip:port",
    )
    parser.add_argument("-e", "--exe", help="Путь к исполняемому файлу (Antigravity или Gemini)")
    parser.add_argument("--gemini", action="store_true", help="Запустить Google Gemini Desktop через прокси")
    parser.add_argument("--app", choices=["antigravity", "gemini"], default=None, help="Выбор приложения (antigravity или gemini)")
    parser.add_argument("--system-proxy", action="store_true", help="Включить системный прокси Windows на время работы")
    parser.add_argument("--patch-accounts", action="store_true", help="Прописать прокси во все аккаунты Antigravity")
    parser.add_argument("--check-only", action="store_true", help="Только проверить прокси")
    parser.add_argument("--bridge-only", action="store_true", help="Запустить только локальный мост")
    parser.add_argument("extra_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    raw_str = args.proxy or RAW_PROXY_STRING
    if not raw_str and (not PROXY_IP or (PROXY_IP == "127.0.0.1" and not PROXY_LOGIN)):
        try:
            inp = input(
                "Вставьте ваш прокси (в формате ip:port:login:password или Enter для настроек из файла): "
            ).strip()
            if inp:
                raw_str = inp
        except EOFError:
            pass

    try:
        upstream = UpstreamProxyConfig(
            raw_input=raw_str if raw_str else None,
            ip=PROXY_IP,
            port=PROXY_PORT,
            login=PROXY_LOGIN,
            password=PROXY_PASSWORD,
        )
    except ValueError as err:
        print(f"[-] Ошибка в данных прокси: {err}")
        return 1

    ok = verify_proxy_and_region(upstream)
    if args.check_only:
        return 0 if ok else 1

    bridge_server, local_bridge_url = start_local_bridge(upstream)
    print(f"\n[+] Локальный мост без пароля активен: {local_bridge_url} -> {upstream.safe_display_url}")

    patch_antigravity_manager_configs(local_bridge_url, patch_accounts=args.patch_accounts)

    target_app = "gemini" if args.gemini else (args.app or "antigravity")
    use_system_proxy = args.system_proxy or (target_app == "gemini")

    if target_app == "gemini":
        exe_path = find_gemini_executable(args.exe)
        app_name = "Google Gemini Desktop"
    else:
        exe_path = find_manager_executable(args.exe)
        app_name = "Antigravity Manager"

    import atexit

    if use_system_proxy:
        actual_port = bridge_server.server_address[1]
        WindowsSystemProxyManager.set_proxy(True, host=LOCAL_BRIDGE_HOST, port=actual_port)
        atexit.register(WindowsSystemProxyManager.set_proxy, False)
        print(f"[+] Системный прокси Windows активирован на 127.0.0.1:{actual_port}")

    if args.bridge_only or not exe_path:
        if not exe_path and not args.bridge_only:
            print(f"[!] Исполняемый файл для {app_name} не найден автоматически.")
        print(f"[*] Мост работает на {local_bridge_url}. (Ctrl+C для выхода).")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            if use_system_proxy:
                WindowsSystemProxyManager.set_proxy(False)
            bridge_server.shutdown()
        return 0

    cmd = [str(exe_path)] + args.extra_args
    print(f"[*] Запуск {app_name}: {exe_path}\n")
    try:
        proc = subprocess.Popen(cmd, env=build_proxy_environment(local_bridge_url))
        return proc.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        if use_system_proxy:
            WindowsSystemProxyManager.set_proxy(False)
        bridge_server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
