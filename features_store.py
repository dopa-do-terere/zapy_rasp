"""Funcoes do local ligadas/desligadas pelo admin no ZAccess (features:sync). Tudo ligado por
padrao: so False desliga. Guardado em features.json para valer com a nuvem fora."""
import json
import os
import threading

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "features.json")
_lock = threading.Lock()


def _read() -> dict:
    try:
        with open(_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(features: dict) -> None:
    clean = {k: v for k, v in (features or {}).items() if isinstance(k, str) and isinstance(v, bool)}
    with _lock:
        tmp = _PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(clean, f)
        os.replace(tmp, _PATH)


def enabled(key: str) -> bool:
    return _read().get(key) is not False


def all_flags() -> dict:
    """Para os templates: features.parcels etc. (chave ausente = ligada)."""
    data = _read()
    return {k: data.get(k) is not False for k in ("portariaNotices", "parcels", "reservations")}
