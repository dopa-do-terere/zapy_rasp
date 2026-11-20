#!/usr/bin/env bash
# Zapy – Instalação: ambiente virtual, dependências e serviço systemd
# Uso: ./install.sh [--no-service]
#   --no-service  só instala venv e dependências (não precisa de sudo)

set -e

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="zapy"
NO_SERVICE=false

for arg in "$@"; do
  case "$arg" in
    --no-service) NO_SERVICE=true ;;
    -h|--help)
      echo "Uso: $0 [--no-service]"
      echo "  Instala venv, dependências e (por padrão) serviço systemd."
      echo "  --no-service  apenas venv + pip install (sem sudo)"
      exit 0
      ;;
  esac
done

echo "[zapy] Diretório: $INSTALL_DIR"
cd "$INSTALL_DIR"

if [[ "$(id -u)" -ne 0 ]] && ! $NO_SERVICE; then
  echo "[zapy] Será pedido sudo ao final para instalar o serviço."
fi

# --- .env (copiar exemplo se não existir) ---
if [[ ! -f ".env" ]] && [[ -f ".env.example" ]]; then
  cp .env.example .env
  echo "[zapy] Arquivo .env criado a partir de .env.example. Ajuste se necessário."
fi

# --- go2rtc (vídeo ao vivo no Cockpit) — opcional: sem ele o zapy funciona, só sem vídeo ---
GO2RTC_VERSION="1.9.4"

# SHA-256 do release oficial v1.9.4 (mesma tabela em scripts/install-windows.ps1 — o
# teste Go2rtcInstallScriptsTest confere que não divergem).
go2rtc_sha256() {
  case "$1" in
    go2rtc_linux_amd64) echo "8d86510e64e0deadee40d550d46323ddec9f62e14cec56de3a9df0f2f7fe9ada" ;;
    go2rtc_linux_arm) echo "a40459ab24d3ce5e329226adc905e5192dffccbb53d42de06d3ee3f2ebf2ebc6" ;;
    go2rtc_linux_arm64) echo "ebea4cf3a0bc3a12190ebba1f2c4b3c8cf4aac099fadb4cd50a669429b074f1c" ;;
    go2rtc_win64.zip) echo "167b64f12cf0ecb3714d2911a4a92c2809c37d43d9f1dd02a0575b6488107816" ;;
  esac
}

install_go2rtc() {
  local bin_dir="$INSTALL_DIR/bin" asset expected tmp
  if [[ -x "$bin_dir/go2rtc" ]] && [[ "$(cat "$bin_dir/go2rtc.version" 2>/dev/null)" == "$GO2RTC_VERSION" ]]; then
    echo "[zapy] go2rtc $GO2RTC_VERSION já instalado."
    return 0
  fi
  case "$(uname -m)" in
    aarch64|arm64) asset="go2rtc_linux_arm64" ;;
    armv7l) asset="go2rtc_linux_arm" ;;
    armv6l)
      echo "[zapy] AVISO: armv6l não suportado pelo vídeo ao vivo — go2rtc não instalado (o resto do zapy funciona)."
      return 0 ;;
    x86_64) asset="go2rtc_linux_amd64" ;;
    *)
      echo "[zapy] AVISO: arquitetura $(uname -m) sem go2rtc — vídeo ao vivo indisponível."
      return 0 ;;
  esac
  expected="$(go2rtc_sha256 "$asset")"
  tmp="$(mktemp)"
  local url="https://github.com/AlexxIT/go2rtc/releases/download/v$GO2RTC_VERSION/$asset"
  echo "[zapy] Baixando go2rtc $GO2RTC_VERSION ($asset)..."
  if command -v curl &>/dev/null; then
    curl -fsSL -o "$tmp" "$url" || { rm -f "$tmp"; echo "[zapy] AVISO: download do go2rtc falhou — vídeo ao vivo indisponível."; return 1; }
  else
    wget -q -O "$tmp" "$url" || { rm -f "$tmp"; echo "[zapy] AVISO: download do go2rtc falhou — vídeo ao vivo indisponível."; return 1; }
  fi
  if ! echo "$expected  $tmp" | sha256sum -c - &>/dev/null; then
    rm -f "$tmp"
    echo "[zapy] AVISO: SHA-256 do go2rtc não confere — NÃO instalado (vídeo ao vivo indisponível)."
    return 1
  fi
  mkdir -p "$bin_dir"
  mv "$tmp" "$bin_dir/go2rtc"
  chmod 755 "$bin_dir/go2rtc"
  echo "$GO2RTC_VERSION" > "$bin_dir/go2rtc.version"
  echo "[zapy] go2rtc $GO2RTC_VERSION instalado em $bin_dir/go2rtc."
}

# Antes do venv/pip: falha de pip (set -e) não pode impedir o vídeo de ser instalado.
install_go2rtc || true

# --- Ambiente virtual e dependências (só como usuário normal) ---
if [[ "$(id -u)" -ne 0 ]]; then
  if [[ ! -d ".venv" ]]; then
    echo "[zapy] Criando ambiente virtual..."
    if ! python3 -m venv .venv 2>/dev/null; then
      echo "[zapy] AVISO: python3 -m venv falhou. Tente: sudo apt install python3-full python3-venv"
      exit 1
    fi
  fi
  echo "[zapy] Instalando dependências Python..."
  "$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
  "$INSTALL_DIR/.venv/bin/pip" install -q -r requirements.txt
  echo "[zapy] Dependências instaladas."
fi

if $NO_SERVICE; then
  echo "[zapy] Concluído (sem serviço). Para rodar: $INSTALL_DIR/.venv/bin/python app.py"
  exit 0
fi

# --- Serviço systemd (requer root) ---
if [[ "$(id -u)" -ne 0 ]]; then
  echo "[zapy] Instalando serviço systemd (sudo)..."
  exec sudo "$0" "$@"
fi

RUN_AS_USER="${SUDO_USER:-root}"
if [[ "$RUN_AS_USER" == "root" ]]; then
  RUN_AS_USER="$(stat -c '%U' "$INSTALL_DIR" 2>/dev/null || echo 'root')"
fi

# --- Pacotes opcionais para GPIO (Raspberry Pi / sensores) ---
if command -v apt-get &>/dev/null; then
  if ! dpkg -l liblgpio-dev &>/dev/null 2>&1; then
    echo "[zapy] Instalando liblgpio-dev (para sensores GPIO)..."
    apt-get update -qq && apt-get install -y liblgpio-dev &>/dev/null || true
  fi
  if getent group gpio &>/dev/null && [[ "$RUN_AS_USER" != "root" ]]; then
    if ! groups "$RUN_AS_USER" 2>/dev/null | grep -q '\bgpio\b'; then
      echo "[zapy] Adicionando usuário $RUN_AS_USER ao grupo gpio (acesso aos sensores)."
      usermod -aG gpio "$RUN_AS_USER" 2>/dev/null || true
    fi
  fi
fi

UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
echo "[zapy] Instalando serviço: $UNIT_FILE (User=$RUN_AS_USER)"

cat > "$UNIT_FILE" << EOF
[Unit]
Description=Zapy - Painel de relés, sensores e cliente ZAccess
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_AS_USER
WorkingDirectory=$INSTALL_DIR
Environment=PATH=$INSTALL_DIR/.venv/bin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile=-$INSTALL_DIR/.env
ExecStart=$INSTALL_DIR/.venv/bin/python app.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl start "$SERVICE_NAME"

# Botao "Atualizar agora" em Configuracoes (sudo restrito a atualizar e reiniciar o zapy).
if [[ "$RUN_AS_USER" != "root" ]]; then
  ZAPY_USER="$RUN_AS_USER" ZAPY_SERVICE="$SERVICE_NAME" bash "$INSTALL_DIR/scripts/instalar-atualizador.sh" || \
    echo "[zapy] AVISO: nao deu para liberar o botao Atualizar (rode scripts/instalar-atualizador.sh com sudo)."
fi

echo ""
echo "[zapy] Instalação concluída."
echo "  Painel:  http://$(hostname -I 2>/dev/null | awk '{print $1}'):3080"
echo "  status:  sudo systemctl status $SERVICE_NAME"
echo "  logs:    journalctl -u $SERVICE_NAME -f"
echo "  parar:   sudo systemctl stop $SERVICE_NAME"
exit 0
