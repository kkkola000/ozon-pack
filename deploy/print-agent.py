#!/usr/bin/env python3
"""Агент печати Ozon Pack.

Панель стоит на сервере в интернете и не может достучаться до принтера в
складской сети. Поэтому агент запускается на любом компьютере рядом с принтером
(подойдёт тот же Mac, с которого работает сборщик), сам забирает задания у
панели и отдаёт их принтеру по TCP. Открывать порты наружу не нужно.

Куда печатать (--printer):
    win:Xprinter XP-420B   принтер Windows по имени (USB) — печать без обработки
    cups:XP-420B           принтер macOS или Linux по имени очереди (USB)
    192.168.1.50:9100      принтер в сети по IP
    /dev/usb/lp0           файл устройства (Linux)
Имя без префикса на Windows считается именем принтера.

Запуск:
    python print-agent.py --url https://panel.example.com ^
        --token КЛЮЧ_ИЗ_НАСТРОЕК --printer "win:Xprinter XP-420B"

Имена доступных принтеров: python3 print-agent.py --list-printers

Только стандартная библиотека Python 3.8+, устанавливать ничего не нужно.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_INTERVAL = 2.0
CONNECT_TIMEOUT = 10
PRINTER_TIMEOUT = 30


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


IS_WINDOWS = sys.platform.startswith("win")


def list_printers() -> int:
    """Показать принтеры системы — чтобы узнать имя для --printer."""
    if IS_WINDOWS:
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Get-Printer | Select-Object -ExpandProperty Name"],
                capture_output=True, text=True, timeout=30,
            )
            names = result.stdout.strip()
        except FileNotFoundError:
            names = ""
        if not names:
            result = subprocess.run(["wmic", "printer", "get", "name"], capture_output=True, text=True, timeout=30)
            names = result.stdout.strip()
        print(names or "Принтеры не найдены")
        print('\nУкажите имя целиком, например: --printer "win:Xprinter XP-420B"')
        return 0

    try:
        result = subprocess.run(["lpstat", "-p", "-d"], capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        print("Команда lpstat не найдена (нужна на macOS и Linux)", file=sys.stderr)
        return 1
    print(result.stdout.strip() or "Принтеры не найдены")
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)
    print("\nДля USB-принтера укажите: --printer cups:ИМЯ_ИЗ_СПИСКА")
    return 0


def send_to_windows(printer_name: str, payload: bytes) -> None:
    """Печать на Windows «как есть» (тип данных RAW) через winspool.

    Идём напрямую в системный API: драйвер не должен ничего интерпретировать,
    принтер ждёт свои команды TSPL. Дополнительных пакетов не требуется.
    """
    if not IS_WINDOWS:
        raise RuntimeError("Печать win: доступна только в Windows")

    import ctypes
    from ctypes import wintypes

    class DocInfo(ctypes.Structure):
        _fields_ = [
            ("pDocName", wintypes.LPWSTR),
            ("pOutputFile", wintypes.LPWSTR),
            ("pDatatype", wintypes.LPWSTR),
        ]

    winspool = ctypes.WinDLL("winspool.drv")
    winspool.OpenPrinterW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.HANDLE), wintypes.LPVOID]
    winspool.OpenPrinterW.restype = wintypes.BOOL
    winspool.StartDocPrinterW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(DocInfo)]
    winspool.StartDocPrinterW.restype = wintypes.DWORD
    winspool.StartPagePrinter.argtypes = [wintypes.HANDLE]
    winspool.StartPagePrinter.restype = wintypes.BOOL
    winspool.WritePrinter.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    winspool.WritePrinter.restype = wintypes.BOOL
    for name in ("EndPagePrinter", "EndDocPrinter", "ClosePrinter"):
        function = getattr(winspool, name)
        function.argtypes = [wintypes.HANDLE]
        function.restype = wintypes.BOOL

    handle = wintypes.HANDLE()
    if not winspool.OpenPrinterW(printer_name, ctypes.byref(handle), None):
        raise RuntimeError(
            f"Принтер «{printer_name}» не найден (код {ctypes.get_last_error() or ctypes.GetLastError()}). "
            "Проверьте имя: python print-agent.py --list-printers"
        )
    try:
        doc = DocInfo("Ozon Pack", None, "RAW")
        if not winspool.StartDocPrinterW(handle, 1, ctypes.byref(doc)):
            raise RuntimeError("Windows не приняла задание печати (StartDocPrinter)")
        try:
            if not winspool.StartPagePrinter(handle):
                raise RuntimeError("Windows не приняла страницу (StartPagePrinter)")
            written = wintypes.DWORD(0)
            buffer = ctypes.create_string_buffer(payload)
            if not winspool.WritePrinter(handle, buffer, len(payload), ctypes.byref(written)):
                raise RuntimeError("Не удалось передать данные принтеру (WritePrinter)")
            if written.value != len(payload):
                raise RuntimeError(f"Принтер принял {written.value} из {len(payload)} байт")
            winspool.EndPagePrinter(handle)
        finally:
            winspool.EndDocPrinter(handle)
    finally:
        winspool.ClosePrinter(handle)


def send_to_cups(queue: str, payload: bytes) -> None:
    """Печать без обработки драйвером: команды принтера должны дойти как есть."""
    result = subprocess.run(
        ["lp", "-d", queue, "-o", "raw", "-"],
        input=payload,
        capture_output=True,
        timeout=PRINTER_TIMEOUT,
    )
    if result.returncode != 0:
        error = (result.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(error or f"lp вернул код {result.returncode}")


def send_to_device(path: str, payload: bytes) -> None:
    with open(path, "wb") as device:
        device.write(payload)
        device.flush()


def send_to_network(host: str, port: int, payload: bytes) -> None:
    with socket.create_connection((host, port), timeout=PRINTER_TIMEOUT) as sock:
        sock.settimeout(PRINTER_TIMEOUT)
        sock.sendall(payload)
        # Даём принтеру дочитать буфер до закрытия соединения
        time.sleep(0.3)


def make_sender(target: str):
    """Выбирает способ доставки по виду адреса принтера."""
    if target.startswith("win:"):
        name = target.split(":", 1)[1]
        return f"принтер Windows «{name}»", lambda payload: send_to_windows(name, payload)
    if target.startswith("cups:"):
        queue = target.split(":", 1)[1]
        return f"очередь CUPS «{queue}»", lambda payload: send_to_cups(queue, payload)
    if target.startswith("/"):
        return f"устройство {target}", lambda payload: send_to_device(target, payload)
    host, _, port = target.rpartition(":")
    if not host or not port.isdigit():
        if IS_WINDOWS:
            # На Windows без префикса это имя принтера, а не адрес
            return f"принтер Windows «{target}»", lambda payload: send_to_windows(target, payload)
        host, port = target, "9100"
    return f"{host}:{port}", lambda payload: send_to_network(host, int(port), payload)


def http_request(url: str, *, data: bytes | None = None, timeout: int = CONNECT_TIMEOUT, insecure: bool = False):
    request = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", "ozon-pack-print-agent/1.0")
    context = None
    if url.startswith("https://") and insecure:
        # Пригодится с самоподписанным сертификатом во внутренней сети
        context = ssl._create_unverified_context()  # noqa: S323
    return urllib.request.urlopen(request, timeout=timeout, context=context)


def main() -> int:
    parser = argparse.ArgumentParser(description="Агент печати этикеток Ozon Pack")
    parser.add_argument("--url", default=os.getenv("OZP_URL", ""), help="адрес панели, например https://panel.example.com")
    parser.add_argument("--token", default=os.getenv("OZP_TOKEN", ""), help="ключ агента из настроек панели")
    parser.add_argument("--printer", default=os.getenv("OZP_PRINTER", ""),
                        help="cups:ИМЯ для USB-принтера, IP[:порт] для сетевого, либо путь к устройству")
    parser.add_argument("--list-printers", action="store_true", help="показать очереди печати и выйти")
    parser.add_argument("--interval", type=float, default=float(os.getenv("OZP_INTERVAL", DEFAULT_INTERVAL)),
                        help="как часто спрашивать задания, секунд")
    parser.add_argument("--insecure", action="store_true", help="не проверять сертификат панели (самоподписанный)")
    parser.add_argument("--once", action="store_true", help="забрать и напечатать одно задание и выйти")
    args = parser.parse_args()

    if args.list_printers:
        return list_printers()

    if not args.url or not args.token or not args.printer:
        parser.print_help()
        print("\nНе хватает --url, --token или --printer", file=sys.stderr)
        return 2

    base = args.url.rstrip("/")
    printer_label, send = make_sender(args.printer)
    token = urllib.parse.quote(args.token, safe="")

    log(f"Панель: {base}")
    log(f"Принтер: {printer_label}")

    # Проверка связи с панелью — сразу видно, верен ли ключ
    try:
        with http_request(f"{base}/api/print/ping?token={token}", insecure=args.insecure) as response:
            state = json.loads(response.read() or b"{}")
        log(f"Связь с панелью есть, заданий в очереди: {state.get('queued', 0)}")
    except urllib.error.HTTPError as exc:
        log(f"Панель отклонила ключ ({exc.code}). Проверьте --token в настройках панели.")
        return 1
    except Exception as exc:  # noqa: BLE001
        log(f"Панель недоступна: {exc}")
        return 1

    idle_logged = False
    while True:
        try:
            with http_request(f"{base}/api/print/next?token={token}", insecure=args.insecure) as response:
                if response.status == 204:
                    if not idle_logged:
                        log("Заданий нет, жду…")
                        idle_logged = True
                    if args.once:
                        return 0
                    time.sleep(args.interval)
                    continue
                job_id = response.headers.get("X-Job-Id", "?")
                payload = response.read()
        except urllib.error.HTTPError as exc:
            log(f"Ошибка панели {exc.code}: {exc.reason}")
            time.sleep(min(30, args.interval * 5))
            continue
        except Exception as exc:  # noqa: BLE001
            log(f"Панель недоступна: {exc}")
            time.sleep(min(30, args.interval * 5))
            continue

        idle_logged = False
        log(f"Задание #{job_id}: {len(payload)} байт -> принтер")
        ok, error = True, None
        try:
            send(payload)
            log(f"Задание #{job_id} напечатано")
        except Exception as exc:  # noqa: BLE001
            ok, error = False, str(exc)
            log(f"Принтер не принял задание #{job_id}: {exc}")

        body = json.dumps({"token": args.token, "job_id": int(job_id), "ok": ok, "error": error}).encode()
        try:
            http_request(f"{base}/api/print/ack", data=body, insecure=args.insecure).close()
        except Exception as exc:  # noqa: BLE001
            log(f"Не удалось отчитаться о задании #{job_id}: {exc}")

        if args.once:
            return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nОстановлено")
