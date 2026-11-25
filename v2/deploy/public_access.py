"""Discover an IPv4 address and add it to local host validation without replacing secrets."""

import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


def validate_ip(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Нужен IPv4 сервера")
    address = ipaddress.IPv4Address(value.strip())
    if address.is_unspecified or address.is_multicast or address.is_loopback or address.is_reserved:
        raise ValueError("Нужен IPv4 сервера")
    return str(address)


def discover_ip() -> str:
    try:
        result = subprocess.run(
            ["ip", "-j", "-4", "address", "show", "scope", "global"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        interfaces = json.loads(result.stdout)
        if not isinstance(interfaces, list):
            raise ValueError("Нет списка интерфейсов")
        for interface in interfaces:
            if not isinstance(interface, dict):
                continue
            entries = interface.get("addr_info", [])
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("family") != "inet":
                    continue
                try:
                    candidate = validate_ip(entry["local"])
                except (ValueError, KeyError, TypeError):
                    continue
                if ipaddress.IPv4Address(candidate).is_global:
                    return candidate
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        pass
    try:
        with urlopen("https://api.ipify.org", timeout=5) as response:
            candidate = validate_ip(response.read(128).decode("ascii"))
        if ipaddress.IPv4Address(candidate).is_global:
            return candidate
    except (OSError, URLError, ValueError, UnicodeError):
        pass
    raise ValueError("Не удалось определить внешний IPv4. Укажите --public-ip АДРЕС")


def configure(path: Path, address: str) -> None:
    # Imported only when configuring an installed app; discovery needs just system Python.
    from pack_v2.infrastructure.config import load_settings

    address = validate_ip(address)
    if path.is_symlink():
        raise ValueError("Конфигурация не должна быть символической ссылкой")
    original = path.stat()
    settings = load_settings(path)
    hosts = list(dict.fromkeys([*settings.allowed_hosts, "localhost", "127.0.0.1", address]))
    replacement = "PACK_V2_ALLOWED_HOSTS=" + json.dumps(hosts, ensure_ascii=True) + "\n"
    lines = path.read_text().splitlines(keepends=True)
    found = False
    updated = []
    for line in lines:
        if re.match(r"^\s*(?:export\s+)?PACK_V2_ALLOWED_HOSTS\s*=", line):
            updated.append(replacement)
            found = True
        else:
            updated.append(line)
    if not found:
        if updated and not updated[-1].endswith("\n"):
            updated[-1] += "\n"
        updated.append(replacement)
    contents = "".join(updated)
    if contents == "".join(lines):
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False) as target:
            temporary = Path(target.name)
            target.write(contents)
            target.flush()
            os.fsync(target.fileno())
        os.chown(temporary, original.st_uid, original.st_gid)
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Доступ к v2 по IP сервера")
    parser.add_argument("--ip", help="IPv4 сервера; без параметра определяется автоматически")
    parser.add_argument("--env-file", type=Path, help="Добавить адрес в существующую конфигурацию")
    args = parser.parse_args()
    try:
        address = validate_ip(args.ip) if args.ip else discover_ip()
        if args.env_file:
            configure(args.env_file, address)
    except (OSError, ValueError):
        print(
            "Не удалось настроить доступ по IPv4. Проверьте конфигурацию или укажите --public-ip.",
            file=sys.stderr,
        )
        return 2
    print(address)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
