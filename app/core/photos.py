"""Фото товаров для листов на бумаге: скачать, уменьшить и не уронить лист.

Панель хранит не фото, а ссылку на него — её отдаёт площадка. Для PDF нужны
сами байты, поэтому фото скачиваются в момент сборки листа и ужимаются до
миниатюры: на бумаге клетка два сантиметра, а исходник бывает в мегабайты.

Фото — украшение листа, а не его суть. Не скачалось, не открылось, слишком
большое, площадка ответила ошибкой — в клетке будет «нет фото», а лист всё
равно соберётся. Ждём тоже не бесконечно: на все фото листа есть общий срок.

Ссылка приходит от площадки, но ходит по ней сервер панели, поэтому по ней
идём не куда угодно: только http(s) и только во внешний интернет — адреса
самого сервера и внутренней сети отсекаются.
"""
from __future__ import annotations

import base64
import binascii
import io
import ipaddress
import logging
import socket
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import urljoin, urlsplit

import httpx

log = logging.getLogger("photos")

TIMEOUT = 6.0             # на одно фото
DEADLINE = 25.0           # на все фото листа сразу
WORKERS = 8
MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
SIDE = 240                # сторона квадратной миниатюры, пикселей
CACHE_SIZE = 1000

_cache: OrderedDict[str, bytes | None] = OrderedDict()
_lock = threading.Lock()


# ------------------------------------------------------------------ куда можно ходить
def _public_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def allowed(url: str) -> bool:
    """Можно ли идти по ссылке: http(s) и не в сторону самого сервера."""
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        return False
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return _public_ip(host)
    # Имя, которое указывает во внутреннюю сеть, — туда тоже не ходим. Не
    # разрешилось — пусть пробует сам запрос: за прокси имена разрешает он.
    try:
        found = socket.getaddrinfo(host, None)
    except OSError:
        return True
    return all(_public_ip(info[4][0]) for info in found)


# ------------------------------------------------------------------ скачивание
def _data_url(url: str) -> bytes | None:
    """Фото, вписанное прямо в ссылку (data:image/…;base64,…)."""
    head, _, body = url.partition(",")
    if not head.startswith("data:image/") or ";base64" not in head:
        return None
    try:
        raw = base64.b64decode(body, validate=False)
    except (binascii.Error, ValueError):
        return None
    return raw if len(raw) <= MAX_BYTES else None


def _download(url: str) -> bytes | None:
    """Байты фото или None. Переадресации проверяем так же, как исходную ссылку."""
    if url.startswith("data:"):
        return _data_url(url)
    with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as client:
        for _hop in range(MAX_REDIRECTS + 1):
            if not allowed(url):
                return None
            with client.stream("GET", url, headers={"Accept": "image/*"}) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers.get("location", ""))
                    continue
                if response.status_code != 200:
                    return None
                if not response.headers.get("content-type", "image/").startswith("image/"):
                    return None
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
    return None


def thumbnail(raw: bytes) -> bytes | None:
    """Квадратная миниатюра JPEG на белом фоне — так её понимает любой PDF."""
    try:
        from PIL import Image, ImageOps
    except ImportError:          # без Pillow лист соберётся, просто без фото
        return None
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            if image.mode in ("RGBA", "LA", "P"):
                image = image.convert("RGBA")
                back = Image.new("RGB", image.size, "white")
                back.paste(image, mask=image.split()[-1])
                image = back
            else:
                image = image.convert("RGB")
            square = ImageOps.pad(image, (SIDE, SIDE), color="white")
            out = io.BytesIO()
            square.save(out, "JPEG", quality=80)
            return out.getvalue()
    except Exception:  # noqa: BLE001 - битое фото не должно ронять лист
        return None


def _one(url: str) -> bytes | None:
    try:
        raw = _download(url)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        log.info("Фото %s не скачалось: %s", url[:120], exc)
        return None
    return thumbnail(raw) if raw else None


def _remember(url: str, value: bytes | None) -> None:
    with _lock:
        _cache[url] = value
        _cache.move_to_end(url)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)


def thumbnails(urls) -> dict[str, bytes]:
    """Миниатюры по ссылкам: {ссылка: JPEG}. Что не получилось — просто нет в ответе.

    Одинаковые ссылки качаются один раз; скачанное помнится до перезапуска —
    лист печатают несколько раз за смену, а фото товара не меняется.
    """
    wanted = list(dict.fromkeys(str(url).strip() for url in urls if url and str(url).strip()))
    found: dict[str, bytes] = {}
    missing = []
    with _lock:
        for url in wanted:
            if url in _cache:
                if _cache[url]:
                    found[url] = _cache[url]
            else:
                missing.append(url)
    if not missing:
        return found
    pool = ThreadPoolExecutor(max_workers=min(WORKERS, len(missing)))
    futures = {pool.submit(_one, url): url for url in missing}
    done, _late = wait(futures, timeout=DEADLINE)
    # Не дождались — не ждём: фото, которые не успели, будут в следующий раз.
    pool.shutdown(wait=False, cancel_futures=True)
    for future in done:
        url = futures[future]
        value = future.result()
        _remember(url, value)
        if value:
            found[url] = value
    return found
