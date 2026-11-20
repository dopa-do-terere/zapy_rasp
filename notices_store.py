"""Avisos dos moradores para a portaria, vindos do ZAccess (notices:sync, notice:new,
notice:updated). Guardados em notices.json para o Cockpit mostrar mesmo com a nuvem fora."""
import json
import os
import threading
from datetime import datetime, timedelta, timezone

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notices.json")
_lock = threading.Lock()
_BRT = timezone(timedelta(hours=-3))


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


def _valid(n) -> bool:
    return isinstance(n, dict) and isinstance(n.get("id"), str) and isinstance(n.get("date"), str)


def replace_all(notices: list) -> None:
    """Lista completa (de hoje em diante) enviada na conexão."""
    with _lock:
        _write({n["id"]: n for n in (notices or []) if _valid(n)})


def upsert(notice: dict) -> None:
    if not _valid(notice):
        return
    with _lock:
        data = _read()
        data[notice["id"]] = notice
        _write(data)


def list_upcoming() -> list:
    """De hoje em diante: pendentes primeiro, depois por data e horário."""
    today = _today()
    items = [n for n in _read().values() if n.get("date", "") >= today]
    return sorted(items, key=lambda n: (n.get("status") != "pending", n.get("date", ""), n.get("time") or "99:99"))
