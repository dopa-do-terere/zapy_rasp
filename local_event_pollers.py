"""
Poller de eventos para terminais cadastrados só no painel local (face_terminals_store,
face_terminals.json) — sem isso, terminal que o servidor não conhece não gera evento
nem foto no painel de Eventos. Roda sempre, com ou sem ZACCESS_SERVER_URL.

Chave é o id LOCAL (o painel já traduz id local -> nome). Eventos só vão pro LocalStore:
o servidor não conhece o id local, então nada é emitido pra ele. Se o mesmo host:port
vier na config do servidor, o servidor tem prioridade e o poller local para.
"""
import logging
import threading
import time

import face_terminals_store
import zaccess_client
from face_agent import EventsPoller, create_face_client, make_events_fetch

logger = logging.getLogger(__name__)


def _fingerprint(t: dict) -> tuple:
    return (t.get("vendor"), t.get("host"), t.get("port"), t.get("username"), t.get("password"),
            t.get("https"), t.get("verify_tls"))


def _host_port(host, port) -> tuple:
    return (str(host), int(port or 80))


def _start_poller(store, local_id: str, fetch) -> threading.Event:
    _, stop = EventsPoller(store, local_id, fetch).start(
        store.add_events, interval_seconds=zaccess_client.FACE_EVENTS_POLL_INTERVAL
    )
    return stop


def sync_local_event_pollers(pollers: dict, store, terminals: list[dict], server_clients: dict,
                             make_client=create_face_client, start_poller=None) -> dict:
    """pollers: local_id -> (fingerprint, stop_event). Para quem sumiu, mudou de config
    ou passou a vir do servidor (mesmo host:port); inicia quem falta e tem poll."""
    start_poller = start_poller or _start_poller
    server_hosts = {
        _host_port(c.terminal.host, c.terminal.port)
        for c in list(server_clients.values()) if getattr(c, "terminal", None) is not None
    }
    wanted = {t["id"]: t for t in terminals if t.get("id")}

    for local_id in list(pollers):
        fingerprint, stop = pollers[local_id]
        t = wanted.get(local_id)
        if t is None or _fingerprint(t) != fingerprint or _host_port(t.get("host"), t.get("port")) in server_hosts:
            stop.set()
            del pollers[local_id]
            logger.info("eventos locais: poller do terminal %s parado", local_id)

    for local_id, t in wanted.items():
        if local_id in pollers or _host_port(t.get("host"), t.get("port")) in server_hosts:
            continue
        try:
            client = make_client(
                t["vendor"], host=t["host"], port=t["port"], username=t["username"], password=t["password"],
                https=t.get("https", False), verify_tls=t.get("verify_tls", True),
                relay_level=t.get("relay_level", 0), group_id=t.get("group_id") or None,
            )
        except Exception as e:
            logger.error("eventos locais: falha ao montar client do terminal %s - %s", local_id, type(e).__name__)
            continue
        fetch = make_events_fetch(client, local_id)
        if fetch is None:
            continue
        pollers[local_id] = (_fingerprint(t), start_poller(store, local_id, fetch))
        logger.info("eventos locais: poller do terminal %s iniciado (%s %s:%s)", local_id, t["vendor"], t["host"], t["port"])
    return pollers


def start_local_event_pollers_in_background(store, interval: float = 30) -> threading.Thread:
    def run():
        pollers: dict = {}
        while True:
            try:
                sync_local_event_pollers(
                    pollers, store, face_terminals_store.list_terminals(),
                    zaccess_client._bridge.get("face_clients") or {},
                )
            except Exception:
                logger.exception("eventos locais: falha no ciclo de sync")
            time.sleep(interval)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread
