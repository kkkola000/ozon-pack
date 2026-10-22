#!/usr/bin/env python3
"""Generate nginx ACLs safely; IP_ALLOWLIST is deployment input, never app policy."""
from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path
import re
import tempfile


def networks(raw: str) -> list:
    result = []
    for value in raw.replace(";", ",").split(","):
        value = value.strip()
        if value:
            result.append(ipaddress.ip_network(value, strict=False))
    return result


def env_allowlist(app_dir: Path) -> str:
    path = app_dir / ".env"
    if not path.exists():
        return ""
    values = [line.partition("=")[2].strip().strip("\"'") for line in path.read_text().splitlines()
              if line.startswith("IP_ALLOWLIST=")]
    if len(values) > 1:
        raise ValueError("В .env несколько IP_ALLOWLIST — оставьте одну строку")
    return values[0] if values else ""


def existing_policy(text: str) -> list | None:
    """Only migrate known allow/deny policies. Never broaden a custom ACL."""
    directives = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            directives.extend(part.strip() for part in line.split(";") if part.strip())
    if directives == ["allow all"]:
        return None
    if not directives or directives[-1] != "deny all":
        raise ValueError("Нестандартное правило nginx: перенесите IP_ALLOWLIST в него вручную")
    result = []
    for rule in directives[:-1]:
        if not rule.startswith("allow ") or rule == "allow all":
            raise ValueError("Нестандартное правило nginx — автоматический перенос остановлен")
        result.extend(networks(rule[6:]))
    return result


def render_policy(app_dir: Path, snippet: Path, requested: str | None = None) -> str:
    raw = env_allowlist(app_dir) if requested is None else requested
    allowed = networks(raw)
    previous = snippet.read_text() if snippet.exists() else None
    if requested is None and not allowed and previous is not None:
        return previous
    if requested is None and allowed and previous is not None:
        old = existing_policy(previous)
        if old is not None:
            # Old access required BOTH nginx and app ACLs. Preserve their intersection.
            allowed = [a if a.prefixlen >= b.prefixlen else b
                       for a in allowed for b in old if a.version == b.version and a.overlaps(b)]
            # An empty intersection must remain deny-all, never become allow-all.
            restricted = True
        else:
            restricted = bool(allowed)
    else:
        restricted = bool(allowed)
    if not restricted:
        return "# Доступ без ограничения IP; вход по логину и паролю.\nallow all;\n"
    allowed.extend(networks("127.0.0.1,::1"))
    collapsed = []
    for version in (4, 6):
        collapsed.extend(ipaddress.collapse_addresses(n for n in allowed if n.version == version))
    return ("# Ограничение IP выполняет только nginx.\n"
            + "".join(f"allow {n.network_address if n.prefixlen == n.max_prefixlen else n};\n"
                      for n in collapsed) + "deny all;\n")


def write_policy(app_dir: Path, snippet: Path, requested: str | None = None) -> None:
    text = render_policy(app_dir, snippet, requested)
    if snippet.exists() and snippet.read_text() == text:
        return
    snippet.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".ozon-access-", dir=snippet.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.chmod(temporary, 0o644)
        os.replace(temporary, snippet)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def verify_site(site: Path, snippet: Path, port: str) -> None:
    """Fail closed on unprotected/custom proxy locations; do not guess nginx inheritance."""
    text = re.sub(r"#.*", "", site.read_text())
    stack: list[list[str]] = [[]]
    found = False
    for token in re.split(r"([{};])", text):
        token = token.strip()
        if not token:
            continue
        if token == "{":
            stack.append([])
        elif token == "}":
            if len(stack) < 2:
                raise ValueError("Некорректные скобки в конфигурации nginx")
            block = stack.pop()
            if any(d.startswith("proxy_pass ") and re.search(
                    rf"http://127\.0\.0\.1:{re.escape(port)}(?:/|$)", d.split()[1]) for d in block):
                found = True
                if f"include {snippet}" not in block:
                    raise ValueError(f"{site}: location панели не подключает {snippet}")
                if any(d.startswith(("allow ", "deny ", "satisfy ")) for d in block):
                    raise ValueError(f"{site}: дополнительные директивы меняют правило доступа")
        elif token != ";":
            stack[-1].append(token)
    if not found:
        raise ValueError(f"{site}: не найден proxy_pass на порт {port}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", type=Path, required=True)
    parser.add_argument("--snippet", type=Path, required=True)
    parser.add_argument("--allow")
    parser.add_argument("--site", type=Path)
    parser.add_argument("--port", default="8080")
    args = parser.parse_args()
    try:
        if args.site:
            verify_site(args.site, args.snippet, args.port)
        else:
            write_policy(args.app_dir, args.snippet, args.allow)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Ошибка настройки доступа nginx: {exc}\n")


if __name__ == "__main__":
    main()

