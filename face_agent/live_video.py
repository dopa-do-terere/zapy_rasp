"""
Vídeo ao vivo do Cockpit: o go2rtc (binário em <repo>/bin, instalado pelo install.sh)
puxa o RTSP do substream do terminal e entrega MSE (H.264 sem recodificar) por WebSocket
em 127.0.0.1:1984; o Flask só faz proxy desse WebSocket, filtrando as mensagens nos dois
sentidos (ver proxy_live e a rota /ws/cockpit/... em app.py).

Segurança: o go2rtc escuta SÓ em 127.0.0.1 (rtsp/webrtc/srtp desligados — sem isso ele
abre 8554/8555 em todas as interfaces). A URL RTSP carrega a senha do terminal: nunca vai
pra argv, arquivo nem log; só pro go2rtc via API local. GET /api/streams do go2rtc devolve
o src com senha — nunca repassar essa rota pelo Flask.
"""
import atexit
import json
import logging
import os
import re
import subprocess
import threading
import time
from urllib.parse import quote

import requests
from simple_websocket import Client as WsClient

logger = logging.getLogger(__name__)

_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BIN_PATH = os.path.join(_REPO_DIR, "bin", "go2rtc.exe" if os.name == "nt" else "go2rtc")

# Substream por padrão (menos banda/CPU no Pi). Intelbras segue o padrão Dahua — a validar
# na bancada; rtsp_path no cadastro do terminal sobrescreve.
_DEFAULT_RTSP_PATHS = {
    "hikvision": "/Streaming/Channels/102",
    "intelbras": "/cam/realmonitor?channel=1&subtype=1",
    "intelbras_biot": "/cam/realmonitor?channel=1&subtype=1",
}

UPSTREAM_IDLE_SECONDS = 15  # go2rtc sem mandar nada por isso: conexão morta, fecha
_CLIENT_MAX_BYTES = 1024
_MSE_VALUE = re.compile(r'[A-Za-z0-9.,;=_\- "/]{1,300}')
_URL_CREDENTIALS = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/]*@")

_HTTP_TIMEOUT = 5
_API_READY_ATTEMPTS = 25  # x 0.2s: go2rtc recém-iniciado demora um pouco pra abrir a API


def build_rtsp_url(t: dict) -> str | None:
    """URL RTSP do terminal (com credencial — não logar), ou None se o fabricante não tem
    vídeo suportado (Control iD e outros)."""
    default_path = _DEFAULT_RTSP_PATHS.get(t.get("vendor"))
    if default_path is None:
        return None
    path = (t.get("rtsp_path") or "").strip() or default_path
    if not path.startswith("/"):
        path = "/" + path
    user = quote(t.get("username") or "", safe="")
    password = quote(t.get("password") or "", safe="")
    return f"rtsp://{user}:{password}@{t['host']}:{int(t.get('rtsp_port') or 554)}{path}"


def scrub(text: str) -> str:
    """Remove credencial de qualquer URL (rtsp://user:senha@ -> rtsp://***@). Tudo que vem
    do go2rtc passa por aqui antes de ir pra log ou pro navegador."""
    return _URL_CREDENTIALS.sub(r"\1***@", str(text))


def classify_error(text: str) -> str:
    """Erro do go2rtc -> mensagem pt-BR pro Cockpit. Strings do go2rtc a confirmar na
    bancada; o que não casar sai genérico (já sem credencial)."""
    low = str(text).lower()
    if any(k in low for k in ("connection refused", "no route", "i/o timeout")):
        return "porta RTSP recusada ou terminal sem resposta"
    if any(k in low for k in ("401", "unauthorized", "user/pass")):
        return "falha de autenticacao RTSP"
    if any(k in low for k in ("h265", "hevc", "hvc1", "hev1")):
        return "terminal em H.265, nao suportado (configure H.264)"
    return "video indisponivel: " + scrub(text)[:120]


def client_message_allowed(data) -> bool:
    """Navegador só pode mandar {"type":"mse","value":"<codecs>"} — nada mais sobe pro go2rtc."""
    if not isinstance(data, str) or len(data.encode("utf-8")) > _CLIENT_MAX_BYTES:
        return False
    try:
        msg = json.loads(data)
    except ValueError:
        return False
    return (
        isinstance(msg, dict) and set(msg) == {"type", "value"} and msg["type"] == "mse"
        and isinstance(msg["value"], str) and _MSE_VALUE.fullmatch(msg["value"]) is not None
    )


def translate_upstream_text(text: str) -> tuple[str | None, int | None]:
    """Texto do go2rtc -> (o que repassar ao navegador ou None, close code ou None).
    `mse` passa como está; `error` nunca passa cru (pode ter rtsp://user:senha@): vira a
    mensagem classificada e fecha 1011; o resto é descartado."""
    try:
        msg = json.loads(text)
    except ValueError:
        return None, None
    if not isinstance(msg, dict):
        return None, None
    if msg.get("type") == "mse":
        return text, None
    if msg.get("type") == "error":
        logger.warning("live_video: go2rtc reportou erro - %s", scrub(msg.get("value"))[:300])
        return json.dumps({"type": "error", "value": classify_error(msg.get("value") or "")}), 1011
    return None, None


def proxy_live(client, upstream, idle_timeout: float = UPSTREAM_IDLE_SECONDS) -> None:
    """Liga o WebSocket do navegador (`client`) ao do go2rtc (`upstream`) até um dos lados
    acabar. Uma thread por sentido; o fim de qualquer lado fecha os dois. Bloqueia."""
    done = threading.Event()
    lock = threading.Lock()

    def finish(code: int, reason: str) -> None:
        with lock:
            if done.is_set():
                return
            done.set()
        for ws, args in ((client, (code, reason)), (upstream, ())):
            try:
                ws.close(*args)
            except Exception:
                pass

    def upstream_to_client() -> None:
        while not done.is_set():
            try:
                msg = upstream.receive(timeout=idle_timeout)
            except Exception:
                return finish(1011, "upstream-closed")
            if msg is None:
                return finish(1011, "upstream-idle")
            try:
                if isinstance(msg, (bytes, bytearray)):
                    client.send(bytes(msg))
                    continue
                forward, code = translate_upstream_text(msg)
                if forward is not None:
                    client.send(forward)
                if code is not None:
                    return finish(code, "upstream-error")
            except Exception:
                return finish(1000, "client-gone")

    def client_to_upstream() -> None:
        while not done.is_set():
            try:
                msg = client.receive(timeout=1)
            except Exception:
                return finish(1000, "bye")
            if msg is None:
                continue  # só pra reparar em `done`
            if not client_message_allowed(msg):
                return finish(1008, "protocol")
            try:
                upstream.send(msg)
            except Exception:
                return finish(1011, "upstream-closed")

    thread = threading.Thread(target=upstream_to_client, daemon=True)
    thread.start()
    client_to_upstream()
    thread.join()


class Go2rtcManager:
    """Sobe/vigia o processo do go2rtc e registra streams nele pela API local."""

    def __init__(self, bin_path: str = DEFAULT_BIN_PATH, api_port: int = 1984,
                 popen=subprocess.Popen, http=requests):
        self.bin_path = bin_path
        self.api_port = api_port
        self.api_url = f"http://127.0.0.1:{api_port}"
        self._popen = popen
        self._http = http
        self._proc = None
        self._streams: dict[str, str] = {}  # nome -> src registrado no processo atual
        self._lock = threading.Lock()
        atexit.register(self.stop)

    @property
    def available(self) -> bool:
        return os.path.isfile(self.bin_path)

    def _config(self) -> str:
        return json.dumps({
            "api": {"listen": f"127.0.0.1:{self.api_port}"},
            "rtsp": {"listen": ""}, "webrtc": {"listen": ""}, "srtp": {"listen": ""},
        })

    def _ensure_running_locked(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        logger.info("live_video: iniciando go2rtc (%s)", self.bin_path)
        self._proc = self._popen(
            [self.bin_path, "-config", self._config()],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._streams.clear()  # processo novo não conhece nenhum stream

    def ensure_running(self) -> None:
        with self._lock:
            self._ensure_running_locked()

    def ensure_stream(self, name: str, src: str) -> None:
        """Registra `src` como stream `name` (idempotente). PUT responde 400 "config file
        disabled" (não há arquivo pra salvar), mas registra — por isso a confirmação é o
        GET /api/streams listar o nome. Exceções daqui podem carregar a URL com a senha:
        quem chama loga só o tipo."""
        with self._lock:
            self._ensure_running_locked()
            if self._streams.get(name) == src:
                return
            streams_url = f"{self.api_url}/api/streams"
            for attempt in range(_API_READY_ATTEMPTS):
                try:
                    if name in self._streams:  # terminal editado: troca o src
                        self._http.delete(streams_url, params={"src": name}, timeout=_HTTP_TIMEOUT)
                    self._http.put(streams_url, params={"name": name, "src": src}, timeout=_HTTP_TIMEOUT)
                    break
                except requests.ConnectionError:
                    if attempt == _API_READY_ATTEMPTS - 1:
                        raise
                    time.sleep(0.2)
            listed = self._http.get(streams_url, timeout=_HTTP_TIMEOUT).json() or {}
            if name not in listed:
                raise RuntimeError(f"go2rtc não registrou o stream {name}")
            self._streams[name] = src

    def connect_ws(self, name: str):
        """WebSocket MSE do go2rtc pro stream `name` (já registrado via ensure_stream)."""
        return WsClient.connect(f"ws://127.0.0.1:{self.api_port}/api/ws?src={quote(name, safe='')}")

    def stop(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()
            self._proc = None
            self._streams.clear()
