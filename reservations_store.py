"""Reservas de espacos do dia, vindas do ZAccess (reservations:sync e reservation:updated).
Guardadas em reservations.json para o Cockpit mostrar mesmo com a nuvem fora. So leitura."""
import json
import os
import threading
from datetime import datetime, timedelta, timezone

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reservations.json")
_lock = threading.Lock()
_BRT = timezone(timedelta(hours=-3))
_HIDDEN = ("rejected", "cancelled")


def _today() -> str:
    return datetime.now(_BRT).strftime("%Y-%m-%d")


def _read() -> dict:
    try:
        with open(_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> None:
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, _PATH)


def _valid(r) -> bool:
    return isinstance(r, dict) and isinstance(r.get("id"), str) and isinstance(r.get("date"), str)


def replace_all(items: list) -> None:
    with _lock:
        _write({r["id"]: r for r in (items or []) if _valid(r)})


def upsert(item: dict) -> None:
    if not _valid(item):
        return
    with _lock:
        data = _read()
        data[item["id"]] = item
        _write(data)


def list_today() -> list:
    today = _today()
    items = [r for r in _read().values() if r.get("date") == today and r.get("status") not in _HIDDEN]
    return sorted(items, key=lambda r: r.get("startTime") or "")
