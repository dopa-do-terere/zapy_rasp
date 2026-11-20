"""Diagnostico dos terminais Control iD cadastrados neste zapy: SecBox/porta, config
RTSP (onvif.*) e o que o servidor RTSP responde ao DESCRIBE. Nao mostra senha.

Uso (no Pi, dentro da pasta do zapy):
  curl -fsSL https://raw.githubusercontent.com/dopa-do-terere/zapy_rasp/main/scripts/diag-controlid.py | .venv/bin/python -
"""
import base64
import hashlib
import re
import socket
import sys

sys.path.insert(0, ".")
import face_terminals_store as store  # noqa: E402
from face_agent.controlid_client import ControlIdClient, ControlIdTerminal  # noqa: E402


def _rtsp(sock, host, port, path, extra=""):
    url = f"rtsp://{host}:{port}{path}"
    sock.sendall((f"DESCRIBE {url} RTSP/1.0\r\nCSeq: 2\r\nAccept: application/sdp\r\n"
                  f"User-Agent: zapy-diag\r\n{extra}\r\n").encode())
    return url, sock.recv(4000).decode(errors="replace")


def _summary(data):
    keep = [l for l in data.split("\r\n") if l.startswith(("RTSP/", "WWW-Authenticate", "m=", "a=rtpmap"))]
    return " | ".join(keep) or repr(data[:200])


def describe(host, port, path, user=None, password=None):
    """DESCRIBE sem senha; com senha faz Digest (o iDFace/LIVE555 pede Digest) ou Basic."""
    try:
        with socket.create_connection((host, port), 5) as s:
            s.settimeout(5)
            url, data = _rtsp(s, host, port, path)
            if user is None or " 401 " not in data.split("\r\n")[0]:
                return _summary(data)
            auth = next((l for l in data.split("\r\n") if l.startswith("WWW-Authenticate")), "")
            if "Digest" in auth:
                f = dict(re.findall(r'(\w+)="([^"]*)"', auth))
                h = lambda x: hashlib.md5(x.encode()).hexdigest()
                resp = h(f"{h(f'{user}:{f["realm"]}:{password}')}:{f['nonce']}:{h(f'DESCRIBE:{url}')}")
                hdr = (f'Authorization: Digest username="{user}", realm="{f["realm"]}", nonce="{f["nonce"]}", '
                       f'uri="{url}", response="{resp}"\r\n')
            else:
                hdr = "Authorization: Basic " + base64.b64encode(f"{user}:{password}".encode()).decode() + "\r\n"
            _, data = _rtsp(s, host, port, path, hdr)
            return _summary(data)
    except Exception as e:
        return f"erro: {e}"

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
