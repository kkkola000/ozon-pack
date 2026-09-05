"""Версия сборки панели.

Номер лежит в файле VERSION, коммит читается из .git — так на сервере всегда
видно, что именно развёрнуто, без запуска git.
"""
from __future__ import annotations

from .config import BASE_DIR

_UNKNOWN = "—"


def get_version() -> str:
    try:
        return (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip() or _UNKNOWN
    except OSError:
        return _UNKNOWN


def get_commit() -> str:
    """Короткий хеш коммита. Читаем файлы .git напрямую: git может быть недоступен."""
    git_dir = BASE_DIR / ".git"
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return _UNKNOWN

    if not head.startswith("ref: "):
        return head[:7] or _UNKNOWN

    ref = head[5:].strip()
    try:
        return (git_dir / ref).read_text(encoding="utf-8").strip()[:7] or _UNKNOWN
    except OSError:
        pass
    # Ветка может лежать не отдельным файлом, а в packed-refs
    try:
        for line in (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return line.split(" ", 1)[0][:7]
    except OSError:
        pass
    return _UNKNOWN


def build_label() -> str:
    commit = get_commit()
    return f"{get_version()}" + (f" ({commit})" if commit != _UNKNOWN else "")
