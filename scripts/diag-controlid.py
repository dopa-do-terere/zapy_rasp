"""Diagnostico dos terminais Control iD cadastrados neste zapy: SecBox/porta, config
RTSP (onvif.*) e o que o servidor RTSP responde ao DESCRIBE. Nao mostra senha.

Uso (no Pi, dentro da pasta do zapy):
  curl -fsSL https://raw.githubusercontent.com/dopa-do-terere/zapy_rasp/main/scripts/diag-controlid.py | .venv/bin/python -
"""
import base64
import socket
import sys

sys.path.insert(0, ".")
import face_terminals_store as store  # noqa: E402
from face_agent.controlid_client import ControlIdClient, ControlIdTerminal  # noqa: E402


def describe(host, port, path, user=None, password=None):
    url = f"rtsp://{host}:{port}{path}"
    head = f"DESCRIBE {url} RTSP/1.0\r\nCSeq: 2\r\nAccept: application/sdp\r\nUser-Agent: zapy-diag\r\n"
    if user is not None:
        head += "Authorization: Basic " + base64.b64encode(f"{user}:{password}".encode()).decode() + "\r\n"
    try:
        with socket.create_connection((host, port), 5) as s:
            s.settimeout(5)
            s.sendall((head + "\r\n").encode())
            data = s.recv(2000).decode(errors="replace")
    except Exception as e:
        return f"erro: {e}"
    lines = [l for l in data.split("\r\n") if l.split(":")[0] in ("RTSP/1.0", "WWW-Authenticate", "Content-Type")
             or l.startswith("RTSP/") or l.startswith("WWW-Authenticate") or l.startswith("m=")]
    return " | ".join(lines) or repr(data[:200])


for t in store.list_terminals():
    if t.get("vendor") != "controlid":
        continue
    print(f"== {t.get('name')} {t['host']}:{t['port']}")
    c = ControlIdClient(ControlIdTerminal(host=t["host"], port=t["port"], username=t["username"],
                                          password=t["password"], https=t["https"], verify_tls=t["verify_tls"]))
    for path, body in (("/doors_state.fcgi", {}),
                       ("/get_configuration.fcgi", {"onvif": ["rtsp_enabled", "rtsp_codec", "rtsp_port", "rtsp_username", "rtsp_rgb"]})):
        try:
            r = c._call(path, json_body=body)
            print(f"  {path} {r.status_code} {r.text[:300]}")
        except Exception as e:
            print(f"  {path} erro: {e}")
    port = int(t.get("rtsp_port") or 554)
    p = (t.get("rtsp_path") or "").strip() or "/main_stream"
    print(f"  DESCRIBE {p} sem senha: {describe(t['host'], port, p)}")
    print(f"  DESCRIBE {p} com senha: {describe(t['host'], port, p, t['username'], t['password'])}")
