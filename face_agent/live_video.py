"""
Vídeo ao vivo do Cockpit: o go2rtc (binário em <repo>/bin, instalado pelo install.sh)
puxa o RTSP do substream do terminal e entrega MP4 fragmentado (H.264 sem recodificar)
em 127.0.0.1:1984; o Flask só repassa esse MP4 (ver app.py, live.mp4).

Segurança: o go2rtc escuta SÓ em 127.0.0.1 (rtsp/webrtc/srtp desligados — sem isso ele
abre 8554/8555 em todas as interfaces). A URL RTSP carrega a senha do terminal: nunca vai
pra argv, arquivo nem log; só pro go2rtc via API local. GET /api/streams do go2rtc devolve
o src com senha — nunca repassar essa rota pelo Flask.
"""
import atexit
import json
import logging
import os
import subprocess
import threading
import time
from urllib.parse import quote

import requests

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

    def stop(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()
            self._proc = None
            self._streams.clear()
