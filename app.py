import csv
import io
import os
import re
import logging
import secrets
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import Flask, render_template, redirect, url_for, request, jsonify, session, Response
from flask_sock import Sock
from functools import wraps

load_dotenv()
from gpiozero import OutputDevice, DigitalInputDevice

from zaccess_client import start_zaccess_client_in_background, submit_controlid_event
from local_event_pollers import start_local_event_pollers_in_background
from config_env import read_config, get_config_for_display, write_config
import face_terminals_store
import notices_store
import zaccess_client
import vehicle_antennas_store
import users_store
from face_agent import FaceProvisioningError, LocalStore, create_face_client
from face_agent.controlid_uhf_client import ControlIdTerminal, ControlIdUhfClient
from face_agent.live_video import DEFAULT_RTSP_PATHS, Go2rtcManager, build_rtsp_url, proxy_live, scrub

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

def _load_or_create_secret_key() -> str:
    """Sem valor padrão fixo de propósito: um literal hardcoded aqui vira uma chave
    conhecida publicamente (está no repositório) — qualquer instalação que suba sem
    ZAPY_SECRET_KEY setado ficaria assinando cookie de sessão com essa string, permitindo
    forjar um cookie de admin sem nunca logar (ex.: flask-unsign). Gera uma chave aleatória
    na primeira vez e persiste num arquivo local (fora do git) — reinícios seguintes reusam
    a mesma, sem precisar o instalador editar nada manualmente."""
    env_key = os.environ.get("ZAPY_SECRET_KEY", "").strip()
    if env_key:
        return env_key
    key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".secret_key")
    if os.path.isfile(key_path):
        with open(key_path, "r", encoding="utf-8") as f:
            existing = f.read().strip()
        if existing:
            return existing
    new_key = secrets.token_hex(32)
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new_key)
    return new_key


app = Flask(__name__, template_folder='painel_rele/templates')
app.secret_key = _load_or_create_secret_key()
# Lax: outro site não manda o cookie do painel em requisição/WS dele (Firefox não aplica Lax
# por padrão). O painel é todo mesma origem, então não quebra nada.
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
sock = Sock(app)

# Mesmo arquivo SQLite (WAL) que zaccess_client.py escreve — conexão própria só de
# leitura pro painel, não depende da thread do cliente ZAccess estar rodando.
local_store = LocalStore()

# Vídeo ao vivo no Cockpit (só terminais do cadastro local) — ver face_agent/live_video.py.
live_video = Go2rtcManager()
_live_lock = threading.Lock()
_live_viewers = 0
LIVE_TEST_TIMEOUT_SECONDS = 10

EVENT_DIRECTION_LABEL = {"in": "Entrada", "out": "Saída", "unknown": "—"}


def login_required(fn):
    """Qualquer usuário logado — admin ou porteiro (ver ROLES em users_store.py)."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)

    return wrapper


def admin_required(fn):
    """Só admin. Porteiro logado cai no cockpit em vez de tomar 403 cru — ele só não
    enxerga rota nenhuma dessas no menu, então isso só protege contra digitar a URL."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        if session.get("role") != "admin":
            return redirect(url_for("cockpit_page"))
        return fn(*args, **kwargs)

    return wrapper

# Configuração dos Relés (Pinos BCM: 5, 6, 13, 19 - canais 1 a 4)
RELAY_PINS = {"1": 5, "2": 6, "3": 13, "4": 19}

# Entradas digitais: 1–4 = Reed Switch NA (porta); 5–8 = Botões. Um fio no GPIO, outro no GND.
SENSOR_PINS = {
    "1": 17, "2": 27, "3": 22, "4": 23,
    "5": 24, "6": 25, "7": 26, "8": 4,
}
SENSOR_PINS_PHYSICAL = {
    "1": 11, "2": 13, "3": 15, "4": 16,
    "5": 18, "6": 22, "7": 37, "8": 7,
}


class _MockSensor:
    """Objeto mock quando GPIO dos sensores não está disponível (ex.: permissão)."""
    value = False


class _MockRelay:
    """Objeto mock quando GPIO não está disponível (ex.: rodando no Windows/sem GPIO)."""
    value = False

    def on(self):
        self.value = True

    def off(self):
        self.value = False

    def toggle(self):
        self.value = not self.value


try:
    reles = {
        id: OutputDevice(pin, active_high=False, initial_value=False)
        for id, pin in RELAY_PINS.items()
    }
except Exception as e:
    logging.warning("Relés GPIO não inicializados (%s). Use modo mock (sem controle físico real).", e)
    reles = {id: _MockRelay() for id in RELAY_PINS}

try:
    sensores = {
        id: DigitalInputDevice(pin, pull_up=True)
        for id, pin in SENSOR_PINS.items()
    }
except Exception as e:
    logging.warning("Sensores GPIO não inicializados (%s). Use modo mock. Verifique permissões (gpio, root) ou pin factory.", e)
    sensores = {id: _MockSensor() for id in SENSOR_PINS}

# ---- Eventos de porta (relé acionado, sensor de porta, botão) no painel de Eventos ----
DOOR_EVENT_SOURCES = ("door", "sensor")
_BUTTON_SENSORS = {"5", "6", "7", "8"}  # 1–4 = reed switch de porta; 5–8 = botões
_sensor_state: dict[str, bool] = {}


def _log_local_event(kind: str, ref: str, text: str, success: bool = True) -> None:
    """Grava no mesmo log dos reconhecimentos (terminal_id "door:N" / "sensor:N")."""
    now = datetime.now(timezone(timedelta(hours=-3)))
    try:
        local_store.add_events([{
            "dedupe_key": f"{kind}:{ref}:{now.strftime('%Y%m%dT%H%M%S%f')}",
            "terminal_id": f"{kind}:{ref}", "employee_no": None, "time": now.isoformat(),
            "direction": "unknown", "success": success, "source": kind,
            "picture": None, "device_name": text[:120],
        }])
    except Exception:
        logging.exception("evento de porta não gravado (%s:%s)", kind, ref)


def _sensor_changed(sid: str, contact_closed: bool) -> None:
    """GPIO com pull-up: ativado = contato fechado (reed: porta fechada; botão: apertado).
    Só registra mudança de verdade (ignora repetição do mesmo estado)."""
    if _sensor_state.get(sid) == contact_closed:
        return
    _sensor_state[sid] = contact_closed
    if sid in _BUTTON_SENSORS:
        if contact_closed:
            _log_local_event("sensor", sid, "Botão pressionado")
        return
    _log_local_event("sensor", sid, "Porta fechada" if contact_closed else "Porta aberta", success=contact_closed)


# Timers de pulso por relé (id -> threading.Timer) para cancelar se acionar de novo
_pulse_timers: dict[str, threading.Timer] = {}
_timers_lock = threading.Lock()


def _get_pulse_seconds(relay_id: str) -> float:
    """Duração do pulso em segundos para o relé (0 = desativado)."""
    val = (os.environ.get(f"PULSE_RELE_{relay_id}") or "").strip()
    try:
        return max(0.0, float(val))
    except ValueError:
        return 0.0


def _close_relay_after_pulse(relay_id: str) -> None:
    with _timers_lock:
        _pulse_timers.pop(relay_id, None)
    if relay_id in reles:
        reles[relay_id].off()


REED_IDS = ("1", "2", "3", "4")   # sensores magnéticos (porta)
BUTTON_IDS = ("5", "6", "7", "8")  # botões (entrada digital)


def _sensor_status() -> dict[str, str]:
    """Estado: 1–4 = Reed (Aberto/Fechado); 5–8 = Botão (Pressionado/Solto)."""
    out = {}
    for sid, s in sensores.items():
        if sid in REED_IDS:
            out[sid] = "Fechado" if s.value else "Aberto"
        else:
            out[sid] = "Pressionado" if not s.value else "Solto"
    return out


@app.route('/')
@admin_required
def index():
    status = {rid: ("LIGADO" if r.value else "DESLIGADO") for rid, r in reles.items()}
    cfg = read_config()
    pulse_config = {str(i): (cfg.get(f"PULSE_RELE_{i}") or "").strip() for i in range(1, 5)}
    all_status = _sensor_status()
    sensor_status_reed = {k: all_status[k] for k in REED_IDS if k in all_status}
    sensor_status_buttons = {k: all_status[k] for k in BUTTON_IDS if k in all_status}
    return render_template(
        'index.html',
        status=status,
        pulse_config=pulse_config,
        sensor_status_reed=sensor_status_reed,
        sensor_status_buttons=sensor_status_buttons,
        sensor_pins=SENSOR_PINS,
        sensor_pins_physical=SENSOR_PINS_PHYSICAL,
    )


# ---- Atualizar o zapy pela página (roda scripts/atualizar-zapy.sh) ----
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_UPDATE_REPO_URL = "https://github.com/dopa-do-terere/zapy_rasp.git"
_UPDATE_SCRIPT = os.path.join(_APP_DIR, "scripts", "atualizar-zapy.sh")
_UPDATER = "/usr/local/sbin/zapy-update"


def _update_log_path() -> str:
    return os.path.join(os.path.expanduser("~"), "zapy-backups", "update-last.log")


def _own_unit() -> tuple[str | None, bool]:
    """(serviço systemd que roda este zapy, é serviço de usuário?) lido do cgroup."""
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as f:
            path = f.read().strip().split(":")[-1]
    except OSError:
        return None, False
    units = [p for p in path.split("/") if p.endswith(".service") and not p.startswith("user@")]
    return (units[-1][:-len(".service")] if units else None), "/user@" in path


def _git(*args) -> str:
    try:
        r = subprocess.run(["git", "-C", _APP_DIR, *args], capture_output=True, text=True, timeout=30)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _update_log_tail(lines: int = 80) -> tuple[str, bool]:
    """(últimas linhas do log, ainda rodando?) — o script sempre termina com "FIM exit=N"."""
    try:
        with open(_update_log_path(), encoding="utf-8", errors="replace") as f:
            text = f.read()[-20000:]
    except OSError:
        return "", False
    tail = "\n".join(text.splitlines()[-lines:])
    return tail, "FIM exit=" not in text


@app.route('/api/system/update', methods=['GET'])
@admin_required
def api_update_status():
    log, running = _update_log_tail()
    return jsonify({"current": _git("log", "-1", "--format=%h %s (%cd)", "--date=format:%d/%m/%Y %H:%M"),
                    "running": running, "log": log})


@app.route('/api/system/update/check', methods=['POST'])
@admin_required
def api_update_check():
    """Compara o commit instalado com a main do GitHub (só leitura)."""
    head = _git("rev-parse", "HEAD")
    remote = _git("ls-remote", _UPDATE_REPO_URL, "refs/heads/main").split("\t")[0]
    if not remote:
        return jsonify({"success": False, "message": "não consegui falar com o GitHub"}), 502
    return jsonify({"success": True, "current": head[:7], "latest": remote[:7], "available": bool(head) and head != remote})


@app.route('/api/system/update', methods=['POST'])
@admin_required
def api_update_start():
    """Roda o atualizar-zapy.sh fora do serviço (systemd-run): o script reinicia o zapy,
    e um processo filho do serviço morreria junto no restart. Precisa de sudo sem senha
    (o mesmo que o script já usa para reiniciar o serviço)."""
    if os.name == "nt":
        return jsonify({"success": False, "message": "atualização pela página só no Linux (Raspberry)"}), 400
    if not os.path.isfile(_UPDATE_SCRIPT):
        return jsonify({"success": False, "message": "scripts/atualizar-zapy.sh não encontrado"}), 500
    if _update_log_tail()[1]:
        return jsonify({"success": False, "message": "já existe uma atualização em andamento"}), 409
    log = _update_log_path()
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(log, "w", encoding="utf-8") as f:
        f.write(f"[zapy] atualização pedida pela página por {session.get('username') or 'admin'} "
                f"em {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n")
    if os.path.isfile(_UPDATER):
        # Comando restrito criado pelo scripts/instalar-atualizador.sh (sudo só para ele).
        cmd = [_UPDATER] if os.geteuid() == 0 else ["sudo", "-n", _UPDATER]
    else:
        import getpass
        unit, user_unit = _own_unit()
        inner = f'bash "{_UPDATE_SCRIPT}" >> "{log}" 2>&1; echo "FIM exit=$?" >> "{log}"'
        env = [f"--setenv=ZAPY_DIR={_APP_DIR}"] + ([f"--setenv=ZAPY_SERVICE={unit}"] if unit else [])
        if user_unit:
            # Serviço de usuário (bancada): roda e reinicia sem sudo.
            cmd = ["systemd-run", "--user", "--unit", f"zapy-update-{int(time.time())}", "--collect", "--quiet",
                   *env, "--setenv=ZAPY_SYSTEMCTL=systemctl --user", f"--working-directory={_APP_DIR}",
                   "/bin/bash", "-c", inner]
        else:
            cmd = ["systemd-run", "--unit", f"zapy-update-{int(time.time())}", "--collect", "--quiet",
                   f"--uid={getpass.getuser()}", f"--setenv=HOME={os.path.expanduser('~')}", *env,
                   f"--working-directory={_APP_DIR}", "/bin/bash", "-c", inner]
            if os.geteuid() != 0:
                cmd = ["sudo", "-n", *cmd]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as e:
        r = None
        err = str(e)
    if r is None or r.returncode != 0:
        err = (r.stderr.strip() if r is not None else err) or "falha ao iniciar"
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"[zapy] não iniciou: {err}\nFIM exit=1\n")
        msg = ("falta liberar o botão: rode uma vez no Pi "
               f"'sudo bash {os.path.join(_APP_DIR, 'scripts', 'instalar-atualizador.sh')}'"
               if "password" in err.lower() or "senha" in err.lower() else err[:200])
        return jsonify({"success": False, "message": msg}), 500
    return jsonify({"success": True}), 202


@app.route('/config')
@admin_required
def config_page():
    """Tela dedicada de configuração do Zapy/ZAccess."""
    return render_template('config.html', go2rtc_available=live_video.available)


@app.route('/face-terminals')
@admin_required
def face_terminals_page():
    """Cadastro dos terminais faciais (Hikvision/Intelbras) que este zapy fala diretamente."""
    return render_template('face_terminals.html', rtsp_defaults=DEFAULT_RTSP_PATHS)


@app.route('/api/face-terminals', methods=['GET'])
@admin_required
def api_face_terminals_list():
    return jsonify([face_terminals_store.for_display(t) for t in face_terminals_store.list_terminals()])


@app.route('/api/face-terminals', methods=['POST'])
@admin_required
def api_face_terminals_create():
    terminal = face_terminals_store.create_terminal(request.get_json() or {})
    return jsonify(face_terminals_store.for_display(terminal)), 201


@app.route('/api/face-terminals/<terminal_id>', methods=['PUT'])
@admin_required
def api_face_terminals_update(terminal_id):
    terminal = face_terminals_store.update_terminal(terminal_id, request.get_json() or {})
    if terminal is None:
        return jsonify({"success": False, "message": "terminal não encontrado"}), 404
    return jsonify(face_terminals_store.for_display(terminal))


@app.route('/api/face-terminals/<terminal_id>', methods=['DELETE'])
@admin_required
def api_face_terminals_delete(terminal_id):
    if not face_terminals_store.delete_terminal(terminal_id):
        return jsonify({"success": False, "message": "terminal não encontrado"}), 404
    return jsonify({"success": True})


def _face_client_for(terminal: dict):
    return create_face_client(
        terminal["vendor"], host=terminal["host"], port=terminal["port"],
        username=terminal["username"], password=terminal["password"],
        https=terminal["https"], verify_tls=terminal["verify_tls"],
        relay_level=terminal.get("relay_level", 0), group_id=terminal.get("group_id") or None,
    )


@app.route('/api/face-terminals/<terminal_id>/live-test', methods=['POST'])
@admin_required
def api_face_terminals_live_test(terminal_id):
    """"Testar vídeo": usa o cadastro salvo, pede MSE H.264 ao go2rtc e devolve o codec ou
    o erro classificado. Nunca devolve nem loga a URL RTSP (tem a senha)."""
    terminal = face_terminals_store.get_terminal(terminal_id)
    if terminal is None:
        return jsonify({"ok": False, "message": "terminal não encontrado"}), 404
    src = build_rtsp_url(terminal)
    if src is None:
        return jsonify({"ok": False, "message": "fabricante sem vídeo ao vivo"})
    if not live_video.available:
        return jsonify({"ok": False, "message": "go2rtc não instalado"})
    if terminal.get("vendor") == "controlid":
        # iDFace vem com RTSP desligado e em MJPEG: liga/ajusta pela API antes de testar.
        try:
            if _face_client_for(terminal).ensure_rtsp(terminal.get("rtsp_port") or 554):
                return jsonify({"ok": False, "message": "vídeo ativado no terminal (RTSP H.264); "
                                "ele está reiniciando, teste de novo em 1 minuto"})
        except Exception as e:
            logging.warning("live: não deu pra configurar o RTSP do Control iD %s - %s", terminal_id, scrub(str(e)))
    try:
        return jsonify(live_video.test_stream(terminal_id, src, timeout=LIVE_TEST_TIMEOUT_SECONDS))
    except Exception as e:
        logging.error("live: falha no teste de vídeo do terminal %s - %s", terminal_id, type(e).__name__)
        return jsonify({"ok": False, "message": "go2rtc não respondeu"})


@app.route('/api/face-terminals/<terminal_id>/test', methods=['POST'])
@admin_required
def api_face_terminals_test(terminal_id):
    """Chama um endpoint leve e sem efeito colateral (system/info ou deviceInfo) só pra
    confirmar que dá pra falar com o terminal com essas credenciais."""
    terminal = face_terminals_store.get_terminal(terminal_id)
    if terminal is None:
        return jsonify({"success": False, "message": "terminal não encontrado"}), 404
    try:
        return jsonify({"success": True, "info": _face_client_for(terminal).check_health()})
    except FaceProvisioningError as e:
        return jsonify({"success": False, "message": str(e)}), 502
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/vehicle-antennas')
@admin_required
def vehicle_antennas_page():
    """Cadastro das antenas veiculares (Control iD iDUHF) que este zapy fala diretamente
    — mesmo papel de face_terminals_page, mas pra antenas."""
    return render_template('vehicle_antennas.html')


@app.route('/api/vehicle-antennas', methods=['GET'])
@admin_required
def api_vehicle_antennas_list():
    return jsonify([vehicle_antennas_store.for_display(a) for a in vehicle_antennas_store.list_antennas()])


@app.route('/api/vehicle-antennas', methods=['POST'])
@admin_required
def api_vehicle_antennas_create():
    antenna = vehicle_antennas_store.create_antenna(request.get_json() or {})
    return jsonify(vehicle_antennas_store.for_display(antenna)), 201


@app.route('/api/vehicle-antennas/<antenna_id>', methods=['PUT'])
@admin_required
def api_vehicle_antennas_update(antenna_id):
    antenna = vehicle_antennas_store.update_antenna(antenna_id, request.get_json() or {})
    if antenna is None:
        return jsonify({"success": False, "message": "antena não encontrada"}), 404
    return jsonify(vehicle_antennas_store.for_display(antenna))


@app.route('/api/vehicle-antennas/<antenna_id>', methods=['DELETE'])
@admin_required
def api_vehicle_antennas_delete(antenna_id):
    if not vehicle_antennas_store.delete_antenna(antenna_id):
        return jsonify({"success": False, "message": "antena não encontrada"}), 404
    return jsonify({"success": True})


def _uhf_client_from_stored(antenna: dict) -> ControlIdUhfClient:
    return ControlIdUhfClient(
        ControlIdTerminal(
            host=antenna["host"], port=antenna["port"],
            username=antenna["username"], password=antenna["password"],
            https=antenna["https"], verify_tls=antenna["verify_tls"],
            group_id=antenna.get("group_id") or None,
        ),
        gate_output=antenna.get("gate_output", "contact"),
        door_id=antenna.get("door_id", 1),
        secbox_id=antenna.get("secbox_id") or None,
    )


@app.route('/api/vehicle-antennas/<antenna_id>/test', methods=['POST'])
@admin_required
def api_vehicle_antennas_test(antenna_id):
    """Mesmo papel de api_face_terminals_test, pra antena UHF."""
    antenna = vehicle_antennas_store.get_antenna(antenna_id)
    if antenna is None:
        return jsonify({"success": False, "message": "antena não encontrada"}), 404
    try:
        client = _uhf_client_from_stored(antenna)
        return jsonify({"success": True, "info": client.check_health()})
    except FaceProvisioningError as e:
        return jsonify({"success": False, "message": str(e)}), 502
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/cockpit/vehicle-antennas/<antenna_id>/open-gate', methods=['POST'])
@login_required
def api_cockpit_vehicle_antenna_open(antenna_id):
    """Abertura remota da cancela direto na antena (local, não passa pelo ZAccess) — mesmo
    caminho de api_cockpit_face_terminal_open, chamando open_gate()."""
    antenna = vehicle_antennas_store.get_antenna(antenna_id)
    if antenna is None:
        return jsonify({"success": False, "message": "antena não encontrada"}), 404
    if session.get("role") != "admin" and not antenna.get("cockpit_enabled", True):
        return jsonify({"success": False, "message": "antena não liberada pro porteiro"}), 403
    try:
        client = _uhf_client_from_stored(antenna)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500
    result = client.open_gate()  # nunca lança — {"ok": bool, "reason"?: str}
    if result.get("ok"):
        now = datetime.now(timezone(timedelta(hours=-3)))
        local_store.add_events([{
            "dedupe_key": f"{antenna_id}:cockpit:{now.strftime('%Y%m%dT%H%M%S%f')}",
            "terminal_id": antenna_id, "employee_no": None, "time": now.isoformat(),
            "direction": "unknown", "success": True, "source": "cockpit",
            "picture": None, "device_name": "Liberado pelo Cockpit",
        }])
        return jsonify({"success": True})
    return jsonify({"success": False, "message": result.get("reason") or "falha ao abrir"}), 502


@app.route('/webhooks/controlid/<terminal_id>/new_user_identified.fcgi', methods=['POST'])
def controlid_webhook_user_identified(terminal_id):
    """Callback que o próprio terminal Control iD chama (modo online/push, diferente dos
    outros vendors que são poll — ver plano de integração, seção Control iD). O terminal
    é configurado manualmente (painel web dele) apontando pra
    http://<ip-deste-zapy>:<porta>/webhooks/controlid/<terminal_id>/new_user_identified.fcgi
    — terminal_id é o id do FaceTerminal no ZAccess (o mesmo que aparece em device:config).
    Sem auth aqui de propósito: a doc oficial não documenta nenhum mecanismo de assinatura
    pro lado do "servidor"; mitigado com allowlist de IP (só aceita evento vindo do próprio
    host cadastrado pro terminal_id) em submit_controlid_event.
    Resposta no formato que a doc mostra o terminal esperando de volta."""
    data = request.get_json(silent=True) or {}
    submit_controlid_event(terminal_id, data, remote_addr=request.remote_addr)
    return jsonify({"result": {"event": data.get("event"), "user_id": data.get("user_id")}})


EVENTS_PAGE_SIZE_DEFAULT = 25
EVENTS_PAGE_SIZE_MAX = 100


def _parse_event_filters() -> dict:
    """Filtros comuns a /api/events e /api/events.csv, lidos da querystring."""
    terminal_id = (request.args.get("terminal") or "").strip() or None
    status = (request.args.get("status") or "").strip().lower()
    success = True if status == "success" else (False if status == "fail" else None)
    start_date = (request.args.get("start") or "").strip()
    end_date = (request.args.get("end") or "").strip()
    # Datas vêm só como YYYY-MM-DD do <input type=date>; -03:00 pra bater com o offset
    # que os eventos já usam (ver _BR_TZ em local_store.py).
    start = f"{start_date}T00:00:00-03:00" if start_date else None
    end = f"{end_date}T23:59:59-03:00" if end_date else None
    q = (request.args.get("q") or "").strip() or None
    return {"terminal_id": terminal_id, "success": success, "start": start, "end": end, "q": q}


def _parse_pagination() -> tuple[int, int]:
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1
    try:
        page_size = int(request.args.get("pageSize") or EVENTS_PAGE_SIZE_DEFAULT)
    except ValueError:
        page_size = EVENTS_PAGE_SIZE_DEFAULT
    return page, max(1, min(page_size, EVENTS_PAGE_SIZE_MAX))


def _resolve_terminal_names() -> dict[str, str]:
    """Nome de cada terminal/antena — prioriza o nome vindo do ZAccess (device:config), cai
    pro cadastro local (face_terminals_store/vehicle_antennas_store), senão mostra o id cru."""
    local_names = {t["id"]: (t.get("name") or t["id"]) for t in face_terminals_store.list_terminals()}
    local_names.update({a["id"]: (a.get("name") or a["id"]) for a in vehicle_antennas_store.list_antennas()})
    ids = set(local_store.list_event_terminal_ids()) | set(local_names.keys())
    for tid in ids:
        kind, _, ref = tid.partition(":")
        if kind == "door":
            local_names.setdefault(tid, f"Porta {ref}")
        elif kind == "sensor":
            local_names.setdefault(tid, f"Botão {ref}" if ref in _BUTTON_SENSORS else f"Sensor de porta {ref}")
    return {tid: (local_store.get_terminal_name(tid) or local_names.get(tid) or tid) for tid in ids}


def _event_to_dict(e, terminal_names: dict[str, str]) -> dict:
    return {
        "dedupeKey": e.dedupe_key,
        "terminalId": e.terminal_id,
        "terminalName": terminal_names.get(e.terminal_id, e.terminal_id),
        "employeeNo": e.employee_no,
        "name": e.name,
        "time": e.time,
        "direction": e.direction,
        "directionLabel": EVENT_DIRECTION_LABEL.get(e.direction, "—"),
        "success": e.success,
        "source": e.source,
        # Evento de porta/sensor não tem pessoa: o "name" é a descrição e não há foto de cadastro.
        "kind": "door" if e.source in DOOR_EVENT_SOURCES else "face",
        "label": (("Acionada" if e.source == "door" else ("Fechada" if e.success else "Aberta"))
                  if e.source in DOOR_EVENT_SOURCES else ("Liberado" if e.success else "Negado")),
        "hasPhoto": e.has_picture or (e.name is not None and e.source not in DOOR_EVENT_SOURCES),
    }


@app.route('/eventos')
@login_required
def events_page():
    """Log de eventos de reconhecimento facial (abertura), com a foto capturada
    na hora (ou a do cadastro, se a captura não estiver disponível)."""
    return render_template('events.html')


@app.route('/api/events')
@login_required
def api_events():
    filters = _parse_event_filters()
    page, page_size = _parse_pagination()
    events, total = local_store.query_events(**filters, limit=page_size, offset=(page - 1) * page_size)
    terminal_names = _resolve_terminal_names()
    return jsonify({
        "events": [_event_to_dict(e, terminal_names) for e in events],
        "total": total,
        "page": page,
        "pageSize": page_size,
        "totalPages": max(1, -(-total // page_size)),
    })


@app.route('/api/events/terminals')
@login_required
def api_events_terminals():
    """Terminais que já têm pelo menos um evento — popula o filtro por terminal."""
    names = _resolve_terminal_names()
    ids = local_store.list_event_terminal_ids()
    return jsonify([{"id": tid, "name": names.get(tid, tid)} for tid in sorted(ids, key=lambda i: names.get(i, i))])


@app.route('/api/events.csv')
@login_required
def api_events_csv():
    """Relatório (CSV) dos eventos filtrados — mesmos filtros de /api/events, sem paginar
    (teto de 5000 linhas, alto o bastante pra não truncar um relatório razoável)."""
    filters = _parse_event_filters()
    events, _ = local_store.query_events(**filters, limit=5000, offset=0)
    terminal_names = _resolve_terminal_names()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Data/hora", "Terminal", "Pessoa", "Matrícula", "Direção", "Resultado"])
    for e in events:
        writer.writerow([
            e.time, terminal_names.get(e.terminal_id, e.terminal_id), e.name or "",
            e.employee_no or "", EVENT_DIRECTION_LABEL.get(e.direction, "-"),
            "Liberado" if e.success else "Negado",
        ])
    # BOM (utf-8-sig): Excel abre acentuação certo sem isso perguntar encoding.
    return Response(
        buf.getvalue().encode("utf-8-sig"), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=eventos_zapy.csv"},
    )


@app.route('/api/events/<path:dedupe_key>/photo')
@login_required
def api_event_photo(dedupe_key):
    """Foto capturada NO MOMENTO do evento (o terminal tira uma foto a cada reconhecimento
    bem-sucedido) — cai pra foto do cadastro da pessoa só se a captura não tiver vindo."""
    jpeg = local_store.get_event_picture(dedupe_key)
    if jpeg is None:
        event = local_store.get_event(dedupe_key)
        if event and event.employee_no:
            jpeg = local_store.get_photo(event.terminal_id, event.employee_no)
    if jpeg is None:
        return '', 404
    return Response(jpeg, mimetype='image/jpeg')


def _cockpit_allowed_relay_ids() -> set[str]:
    """IDs de relé liberados pro porteiro — todos por padrão (COCKPIT_RELAYS vazio/ausente),
    até o admin restringir em /config. "none" é o marcador explícito de "nenhum liberado"
    (senão fica ambíguo com "vazio = todos" quando o admin desmarca tudo)."""
    raw = (read_config().get("COCKPIT_RELAYS") or "").strip()
    if not raw:
        return set(RELAY_PINS.keys())
    if raw == "none":
        return set()
    return {p.strip() for p in raw.split(",") if p.strip()} & set(RELAY_PINS.keys())


@app.route('/cockpit')
@login_required
def cockpit_page():
    """Painel operacional pro porteiro: abrir relés/terminais e ver eventos — sem os
    detalhes de baixo nível (GPIO cru, sensores, config) do painel admin. Admin vê tudo
    (mesmo o que está desmarcado pro porteiro); porteiro só vê o que foi liberado."""
    is_admin = session.get("role") == "admin"
    allowed_relays = set(RELAY_PINS.keys()) if is_admin else _cockpit_allowed_relay_ids()
    relays = [{"id": rid, "pulseSeconds": _get_pulse_seconds(rid)} for rid in RELAY_PINS if rid in allowed_relays]
    # Só o que o Cockpit precisa (nada de credencial): id, nome, host e se tem vídeo ao vivo
    # (com o motivo quando não tem — vira a mensagem do fallback).
    terminals = []
    for t in face_terminals_store.list_terminals():
        if not (is_admin or t.get("cockpit_enabled", True)):
            continue
        reason = _live_unavailable_reason(t)
        terminals.append({"id": t["id"], "name": t.get("name") or t.get("host"), "host": t.get("host"),
                          "live": reason is None, "reason": reason})
    antennas = [
        vehicle_antennas_store.for_display(a) for a in vehicle_antennas_store.list_antennas()
        if is_admin or a.get("cockpit_enabled", True)
    ]
    return render_template('cockpit.html', relays=relays, terminals=terminals, antennas=antennas)


@app.route('/api/cockpit/notices')
@login_required
def api_cockpit_notices():
    """Avisos dos moradores (de hoje em diante) que vieram do ZAccess."""
    return jsonify({"notices": notices_store.list_upcoming()})


@app.route('/api/cockpit/notices/<notice_id>/ack', methods=['POST'])
@login_required
def api_cockpit_notice_ack(notice_id):
    """Porteiro marca "Ciente": vai para o ZAccess, que avisa o morador e devolve
    notice:updated (atualiza a lista)."""
    if not re.fullmatch(r"[0-9a-f]{24}", notice_id or ""):
        return jsonify({"success": False, "message": "aviso inválido"}), 400
    ack = zaccess_client._bridge.get("ack_notice")
    if not ack or not ack(notice_id, session.get("username") or "Portaria"):
        return jsonify({"success": False, "message": "sem conexão com o ZAccess; tente de novo"}), 503
    return jsonify({"success": True})


@app.route('/api/cockpit/relays/<id>/open', methods=['POST'])
@login_required
def api_cockpit_relay_open(id):
    """Sempre ABRE (nunca fecha um relé já aberto, ao contrário de /toggle) — é o botão
    do porteiro, não faz sentido ele "fechar a porta" sem querer clicando de novo."""
    if id not in reles:
        return jsonify({"success": False, "message": "relé não encontrado"}), 404
    if session.get("role") != "admin" and id not in _cockpit_allowed_relay_ids():
        return jsonify({"success": False, "message": "relé não liberado pro porteiro"}), 403
    sec = _get_pulse_seconds(id)
    with _timers_lock:
        old = _pulse_timers.pop(id, None)
        if old:
            old.cancel()
    reles[id].on()
    if sec > 0:
        t = threading.Timer(sec, _close_relay_after_pulse, args=[id])
        t.daemon = True
        with _timers_lock:
            _pulse_timers[id] = t
        t.start()
    _log_local_event("door", id, f"Aberta pelo Cockpit ({session.get('username') or 'porteiro'})")
    return jsonify({"success": True, "pulseSeconds": sec})


@app.route('/api/cockpit/face-terminals/<terminal_id>/open', methods=['POST'])
@login_required
def api_cockpit_face_terminal_open(terminal_id):
    """Abertura remota direto no terminal facial (local, não passa pelo ZAccess) —
    mesmo caminho de /api/face-terminals/<id>/test, mas chamando open_door()."""
    terminal = face_terminals_store.get_terminal(terminal_id)
    if terminal is None:
        return jsonify({"success": False, "message": "terminal não encontrado"}), 404
    if session.get("role") != "admin" and not terminal.get("cockpit_enabled", True):
        return jsonify({"success": False, "message": "terminal não liberado pro porteiro"}), 403
    try:
        client = create_face_client(
            terminal["vendor"], host=terminal["host"], port=terminal["port"],
            username=terminal["username"], password=terminal["password"],
            https=terminal["https"], verify_tls=terminal["verify_tls"],
            relay_level=terminal.get("relay_level", 0), group_id=terminal.get("group_id") or None,
        )
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500
    result = client.open_door()  # nunca lança — {"ok": bool, "reason"?: str}
    if result.get("ok"):
        # Registra no log de Eventos igual um reconhecimento facial — sem isso a abertura
        # pelo cockpit fica invisível no painel (não passa por reconhecimento, não gera
        # AcsEvent/doorlog nenhum pra poller pegar). device_name fixo em vez de tentar
        # inferir da resposta do device: aqui sempre foi o porteiro clicando, não tem
        # ambiguidade a resolver como no doorlog do XPE.
        now = datetime.now(timezone(timedelta(hours=-3)))
        capture_snapshot = getattr(client, "capture_snapshot", None)
        local_store.add_events([{
            "dedupe_key": f"{terminal_id}:cockpit:{now.strftime('%Y%m%dT%H%M%S%f')}",
            "terminal_id": terminal_id, "employee_no": None, "time": now.isoformat(),
            "direction": "unknown", "success": True, "source": "cockpit",
            "picture": capture_snapshot() if capture_snapshot else None,
            "device_name": "Liberado pelo Cockpit",
        }])
    return jsonify({"success": bool(result.get("ok")), "reason": result.get("reason")}), (200 if result.get("ok") else 502)


def _live_unavailable_reason(terminal: dict) -> str | None:
    """None = vídeo ao vivo disponível pro terminal; senão o motivo (mensagem do Cockpit).
    Mesmas regras da rota WS (_handle_live_ws)."""
    if not _live_enabled_globally():
        return "vídeo ao vivo desligado"
    if not terminal.get("live_enabled", True):
        return "vídeo desligado neste terminal"
    if build_rtsp_url(terminal) is None:
        return "fabricante sem vídeo ao vivo"
    if not live_video.available:
        return "go2rtc não instalado"
    return None


def _live_max_viewers() -> int:
    """ZAPY_LIVE_MAX_VIEWERS no .env — 0/vazio/inválido = ilimitado."""
    try:
        return max(0, int((os.environ.get("ZAPY_LIVE_MAX_VIEWERS") or "0").strip()))
    except ValueError:
        return 0


def _live_enabled_globally() -> bool:
    """ZAPY_LIVE_ENABLED no .env — "0" desliga; vazio/ausente = ativo."""
    return (os.environ.get("ZAPY_LIVE_ENABLED") or "").strip() != "0"


def _acquire_live_slot():
    """Ocupa uma vaga de ZAPY_LIVE_MAX_VIEWERS (conexões WS abertas, miniaturas contam).
    Retorna a função que libera a vaga (idempotente), ou None se está no limite."""
    global _live_viewers
    limit = _live_max_viewers()
    with _live_lock:
        if limit and _live_viewers >= limit:
            return None
        _live_viewers += 1
    released = threading.Event()

    def release():
        global _live_viewers
        with _live_lock:
            if released.is_set():
                return
            released.set()
            _live_viewers -= 1

    return release


def _handle_live_ws(ws, terminal_id):
    """Proxy MSE do Cockpit. flask-sock completa o handshake antes daqui, então recusa vira
    close code: 4401 sem login, 4403 sem permissão/vídeo desligado, 4404 sem terminal/RTSP/
    go2rtc, 4429 limite. Nunca loga a URL RTSP (tem a senha)."""
    # Origem primeiro: WS não tem CORS, então outro site aberto no navegador do porteiro
    # conseguiria abrir esta conexão com o cookie dele. Origin ausente = cliente que não é
    # navegador, segue as demais regras.
    origin = request.headers.get("Origin")
    if origin and urlparse(origin).netloc != request.host:
        return ws.close(4403, "origem")
    if not session.get("logged_in"):
        return ws.close(4401, "login")
    terminal = face_terminals_store.get_terminal(terminal_id)
    if terminal is None:
        return ws.close(4404, "terminal")
    if session.get("role") != "admin" and not terminal.get("cockpit_enabled", True):
        return ws.close(4403, "cockpit")
    if not _live_enabled_globally() or not terminal.get("live_enabled", True):
        return ws.close(4403, "desligado")
    src = build_rtsp_url(terminal)
    if src is None or not live_video.available:
        return ws.close(4404, "sem video")
    release = _acquire_live_slot()
    if release is None:
        return ws.close(4429, "limite")
    try:
        live_video.ensure_stream(terminal_id, src)
        upstream = live_video.connect_ws(terminal_id)
        proxy_live(ws, upstream)
    except Exception as e:
        logging.error("live: falha no vídeo do terminal %s - %s", terminal_id, type(e).__name__)
        try:
            ws.close(1011, "go2rtc")
        except Exception:
            pass
    finally:
        release()


@sock.route('/ws/cockpit/face-terminals/<terminal_id>/live')
def ws_cockpit_face_terminal_live(ws, terminal_id):
    _handle_live_ws(ws, terminal_id)


@app.route('/usuarios')
@admin_required
def users_page():
    """Contas de acesso ao painel (porteiro etc.) além do admin de bootstrap — ver
    users_store.py."""
    return render_template('users.html')


@app.route('/api/users', methods=['GET'])
@admin_required
def api_users_list():
    return jsonify([users_store.for_display(u) for u in users_store.list_users()])


@app.route('/api/users', methods=['POST'])
@admin_required
def api_users_create():
    try:
        user = users_store.create_user(request.get_json() or {})
        return jsonify(users_store.for_display(user)), 201
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)}), 400


@app.route('/api/users/<user_id>', methods=['PUT'])
@admin_required
def api_users_update(user_id):
    user = users_store.update_user(user_id, request.get_json() or {})
    if user is None:
        return jsonify({"success": False, "message": "usuário não encontrado"}), 404
    return jsonify(users_store.for_display(user))


@app.route('/api/users/<user_id>', methods=['DELETE'])
@admin_required
def api_users_delete(user_id):
    if session.get("username") and users_store.get_user(user_id) and \
            users_store.get_user(user_id)["username"] == session["username"]:
        return jsonify({"success": False, "message": "não dá pra remover o próprio usuário logado"}), 400
    if not users_store.delete_user(user_id):
        return jsonify({"success": False, "message": "usuário não encontrado"}), 404
    return jsonify({"success": True})


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = (request.form.get('username') or "").strip()
        password = (request.form.get('password') or "").strip()
        # Admin de bootstrap (sempre existe, mesmo com users.json vazio/ausente — não dá
        # pra ficar sem acesso nenhum). Contas extras (porteiro, outros admins) via /usuarios.
        bootstrap_user = os.environ.get("ZAPY_ADMIN_USER", "admin")
        bootstrap_pass = os.environ.get("ZAPY_ADMIN_PASSWORD", "zroot")
        stored = users_store.find_by_username(username)

        role = None
        if username == bootstrap_user and password == bootstrap_pass:
            role = "admin"
        elif stored and stored.get("active", True) and users_store.verify_password(stored, password):
            role = stored["role"]

        if role:
            session["logged_in"] = True
            session["role"] = role
            session["username"] = username
            default_next = url_for("index") if role == "admin" else url_for("cockpit_page")
            return redirect(request.args.get("next") or default_next)
        error = "Usuário ou senha inválidos."
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/health')
@admin_required
def health_page():
    return render_template('health.html')


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


@app.route('/api/health-local')
@admin_required
def api_health_local():
    """Saúde deste zapy (só stdlib; /proc e /sys no Linux, o resto vira null)."""
    import platform
    import shutil
    import socket as _socket
    mem = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        if v.strip().split() and v.strip().split()[0].isdigit():
            mem[k] = int(v.split()[0]) * 1024
    try:
        load = os.getloadavg()
    except OSError:
        load = None
    disk = shutil.disk_usage(_APP_DIR)
    uptime = _read("/proc/uptime").split()
    temp = _read("/sys/class/thermal/thermal_zone0/temp").strip()
    os_name = next((l.split("=", 1)[1].strip('"') for l in _read("/etc/os-release").splitlines() if l.startswith("PRETTY_NAME=")), "")
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
    except OSError:
        ip = None
    last, _ = local_store.query_events(limit=1)
    connected = zaccess_client._bridge.get("connected")
    return jsonify({
        "hostname": platform.node(),
        "ip": ip,
        "model": _read("/proc/device-tree/model").strip("\x00\n ") or platform.machine(),
        "os": os_name or f"{platform.system()} {platform.release()}",
        "cpus": os.cpu_count(),
        "load": [round(x, 2) for x in load] if load else None,
        "memTotal": mem.get("MemTotal"), "memAvailable": mem.get("MemAvailable"),
        "diskTotal": disk.total, "diskFree": disk.free,
        "uptimeSeconds": int(float(uptime[0])) if uptime else None,
        "cpuTempC": round(int(temp) / 1000, 1) if temp.isdigit() else None,
        "version": _git("log", "-1", "--format=%h (%cd)", "--date=format:%d/%m/%Y"),
        "zaccessUrl": (read_config().get("ZACCESS_SERVER_URL") or "").strip() or None,
        "zaccessConnected": bool(connected()) if connected else False,
        "go2rtc": bool(live_video.available),
        "faceTerminals": len(face_terminals_store.list_terminals()),
        "lastEvent": last[0].time if last else None,
        "sensors": _sensor_status(),
        "relays": {rid: bool(r.value) for rid, r in reles.items()},
    })


@app.route('/api/sensors')
@admin_required
def api_sensors():
    """Retorna o estado atual dos 8 entradas (4 reed + 4 botões) para polling."""
    return jsonify(_sensor_status())


@app.route('/toggle/<id>', methods=['POST'])
@admin_required
def toggle(id):
    if id not in reles:
        return redirect(url_for('index'))
    sec = _get_pulse_seconds(id)
    if sec > 0:
        with _timers_lock:
            old = _pulse_timers.pop(id, None)
            if old:
                old.cancel()
        reles[id].on()
        t = threading.Timer(sec, _close_relay_after_pulse, args=[id])
        t.daemon = True
        with _timers_lock:
            _pulse_timers[id] = t
        t.start()
    else:
        reles[id].toggle()
    _log_local_event("door", id, f"Acionada pelo painel ({session.get('username') or 'admin'})")
    return redirect(url_for('index'))


# --- Configuração ZAccess (variáveis de ambiente) ---
@app.route('/api/config', methods=['GET'])
@admin_required
def api_config_get():
    """Retorna as variáveis de ambiente do ZAccess para o frontend (token mascarado)."""
    return jsonify(get_config_for_display())


@app.route('/api/config', methods=['POST'])
@admin_required
def api_config_post():
    """Atualiza variáveis de ambiente no .env e recarrega load_dotenv (não reinicia o processo)."""
    try:
        data = request.get_json() or {}
        write_config(data)
        load_dotenv(override=True)
        return jsonify({"success": True, "message": "Configuração salva. Reinicie o serviço para aplicar a conexão com o ZAccess."})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/restart', methods=['POST'])
@admin_required
def api_restart():
    """
    Agenda reinício do serviço systemd (zapy). Responde antes de executar.
    Requer permissão: sudo systemctl restart zapy (ex.: sudoers NOPASSWD).
    """
    service_name = os.environ.get("ZAPY_SERVICE_NAME", "zapy")

    def do_restart():
        time.sleep(1.5)
        try:
            subprocess.run(
                ["sudo", "systemctl", "restart", service_name],
                capture_output=True,
                timeout=10,
            )
        except Exception as e:
            logging.warning("Falha ao reiniciar serviço: %s", e)

    threading.Thread(target=do_restart, daemon=True).start()
    return jsonify({
        "success": True,
        "message": "Reinício do serviço em andamento. A página pode desconectar em instantes.",
    }), 202


if __name__ == '__main__':
    # Conecta ao ZAccess; envia relés e estado dos sensores/botões (inputs)
    for _sid, _dev in sensores.items():
        if hasattr(_dev, "when_activated"):
            _sensor_state[_sid] = bool(_dev.value)  # estado inicial: não vira evento
            _dev.when_activated = lambda i=_sid: _sensor_changed(i, True)
            _dev.when_deactivated = lambda i=_sid: _sensor_changed(i, False)
    zaccess_client._bridge["on_sensor"] = _sensor_changed
    zaccess_client._bridge["on_relay"] = lambda rid, text: _log_local_event("door", rid, text)
    start_zaccess_client_in_background(reles, sensores=sensores, sensor_pins=SENSOR_PINS)
    # Eventos/fotos dos terminais do cadastro local — sempre, com ou sem ZACCESS_SERVER_URL
    start_local_event_pollers_in_background(local_store)
    # Painel local (porta do .env ou 3080)
    port_str = (os.environ.get("PORT") or "3080").strip()
    port = int(port_str) if port_str else 3080
    app.run(host='0.0.0.0', port=port)