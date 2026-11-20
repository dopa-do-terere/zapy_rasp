"""Encomendas esperadas (aviso do morador) e aguardando retirada, vindas do ZAccess (parcels:sync, parcel:new,
parcel:updated). Guardadas em parcels.json para o Cockpit mostrar mesmo com a nuvem fora.
O código de retirada nunca vem para o zapy: só o morador vê no app."""
import json
import os
import threading

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parcels.json")
_lock = threading.Lock()
_OPEN = ("waiting", "expected")


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


def _valid(p) -> bool:
    return isinstance(p, dict) and isinstance(p.get("id"), str) and isinstance(p.get("unit"), str)


def replace_all(data: dict) -> None:
    """Na conexão: encomendas aguardando, unidades do local e empresas."""
    data = data or {}
    parcels = {p["id"]: p for p in (data.get("parcels") or []) if _valid(p) and p.get("status") in _OPEN}
    units = [u for u in (data.get("units") or []) if isinstance(u, str)]
    carriers = [c for c in (data.get("carriers") or []) if isinstance(c, dict) and isinstance(c.get("value"), str)]
    with _lock:
        _write({"parcels": parcels, "units": units, "carriers": carriers})


def upsert(parcel: dict) -> None:
    """Nova ou atualizada; entregue ou cancelada sai da lista."""
    if not _valid(parcel):
        return
    with _lock:
        data = _read()
        parcels = data.get("parcels") if isinstance(data.get("parcels"), dict) else {}
        if parcel.get("status") in _OPEN:
            parcels[parcel["id"]] = parcel
        else:
            parcels.pop(parcel["id"], None)
        data["parcels"] = parcels
        _write(data)


def snapshot() -> dict:
    """Na portaria (mais antigas primeiro), depois esperadas; unidades e empresas."""
    data = _read()
    parcels = data.get("parcels") if isinstance(data.get("parcels"), dict) else {}
    return {
        "parcels": sorted(parcels.values(), key=lambda p: (p.get("status") == "expected", p.get("receivedAt") or p.get("createdAt") or "")),
        "units": data.get("units") or [],
        "carriers": data.get("carriers") or [],
    }
