#!/usr/bin/env bash
# Atualiza o zapy rodando NO PROPRIO Raspberry.
#
# Uso (no Pi), direto do GitHub:
#   curl -fsSL https://raw.githubusercontent.com/dopa-do-terere/zapy_rasp/main/scripts/atualizar-zapy.sh | bash
# Com versao especifica ou sem internet (git bundle):
#   bash atualizar-zapy.sh [origem] [ref]
#     origem  URL git ou arquivo .bundle (padrao: GitHub do zapy_rasp)
#     ref     branch ou tag (padrao: main)
#
# Pasta: ZAPY_DIR, ou a pasta atual se tiver o zapy, ou ~/zapy, ou ~/zapy_rasp.
# Servico: ZAPY_SERVICE, ou o unit systemd cujo WorkingDirectory e a pasta, ou "zapy".
# ZAPY_SYSTEMCTL="systemctl --user" para servico de usuario (padrao: sudo systemctl).
#
# Faz: backup (tar sem .venv) em ~/zapy-backups, atualiza os arquivos versionados
# (.env, json, sqlite e demais dados locais ficam), pip install se precisar, restart
# do servico e, se o servico nao subir, volta sozinho para o estado anterior.
# Instalacao copiada sem git e convertida para git na primeira vez.
set -euo pipefail

SRC="${1:-https://github.com/dopa-do-terere/zapy_rasp.git}"
REF="${2:-main}"
SC="${ZAPY_SYSTEMCTL:-sudo systemctl}"

command -v git >/dev/null || { echo "[zapy] instale o git: sudo apt install -y git"; exit 1; }

is_zapy() { [ -f "$1/app.py" ] && [ -f "$1/zaccess_client.py" ]; }
if [ -z "${ZAPY_DIR:-}" ]; then
  for d in "$PWD" "$HOME/zapy" "$HOME/zapy_rasp"; do
    if is_zapy "$d"; then ZAPY_DIR="$d"; break; fi
  done
fi
[ -n "${ZAPY_DIR:-}" ] && is_zapy "$ZAPY_DIR" || { echo "[zapy] pasta do zapy nao encontrada; rode dentro dela ou use ZAPY_DIR=..."; exit 1; }
ZAPY_DIR="$(cd "$ZAPY_DIR" && pwd)"

if [ -f "$SRC" ]; then
  SRC="$(realpath "$SRC")"
  git bundle verify -q "$SRC" >/dev/null
fi
cd "$ZAPY_DIR"
echo "[zapy] pasta: $ZAPY_DIR  origem: $SRC ($REF)"

OLD=""
if [ -d .git ]; then
  if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "[zapy] ha arquivos versionados alterados; nada foi feito:"
    git status --short --untracked-files=no
    exit 1
  fi
  OLD="$(git rev-parse HEAD)"
fi

TMPGIT=""
if [ -z "$OLD" ]; then
  # Sem git: busca num repositorio temporario so para saber o commit.
  TMPGIT="$(mktemp -d)"
  git -C "$TMPGIT" init -q
  git -C "$TMPGIT" fetch -q "$SRC" "$REF"
  NEW="$(git -C "$TMPGIT" rev-parse FETCH_HEAD)"
else
  git fetch -q "$SRC" "$REF"
  NEW="$(git rev-parse FETCH_HEAD)"
  if [ "$OLD" = "$NEW" ]; then
    echo "[zapy] ja esta em ${NEW:0:7}; nada a fazer"
    exit 0
  fi
  if ! git merge-base --is-ancestor "$OLD" "$NEW"; then
    echo "[zapy] ${OLD:0:7} nao e ancestral de ${NEW:0:7} (historico divergiu); nada foi feito"
    exit 1
  fi
fi


# Usa sempre o atualizador da versao nova (correcoes do proprio script valem ja nesta atualizacao).
if [ -z "${ZAPY_REEXEC:-}" ]; then
  _new_script="$(mktemp)"
  if git -C "${TMPGIT:-$ZAPY_DIR}" show "$NEW:scripts/atualizar-zapy.sh" > "$_new_script" 2>/dev/null \
     && ! cmp -s "$_new_script" "$ZAPY_DIR/scripts/atualizar-zapy.sh"; then
    echo "[zapy] usando o atualizador da versao ${NEW:0:7}"
    ZAPY_REEXEC=1 ZAPY_DIR="$ZAPY_DIR" exec bash "$_new_script" "$SRC" "$REF"
  fi
  rm -f "$_new_script"
fi
# ---- Servico systemd do zapy: primeiro pelo processo rodando (cgroup), depois pelos units ----
unit_from_process() {
  local pid cg u
  for pid in $(pgrep -f "app.py" 2>/dev/null); do
    [ "$(readlink "/proc/$pid/cwd" 2>/dev/null)" = "$ZAPY_DIR" ] || continue
    cg="$(tail -1 "/proc/$pid/cgroup" 2>/dev/null | cut -d: -f3)"
    u="$(printf '%s' "$cg" | tr '/' '\n' | grep '\.service$' | grep -v '^user@' | tail -1 || true)"
    [ -n "$u" ] || continue
    ZAPY_SERVICE="${u%.service}"
    case "$cg" in */user@*) [ -z "${ZAPY_SYSTEMCTL:-}" ] && SC="systemctl --user";; esac
    return 0
  done
  return 1
}
unit_exists() { ${SC#sudo } list-unit-files "$1.service" --no-legend 2>/dev/null | grep -q .; }
user_bus() {
  case "$SC" in *--user*)
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}";;
  esac
}
if [ -n "${ZAPY_SERVICE:-}" ]; then user_bus; fi
if [ -z "${ZAPY_SERVICE:-}" ] || ! unit_exists "$ZAPY_SERVICE"; then
  ZAPY_SERVICE=""
  if ! unit_from_process; then
    unit="$(grep -lx "WorkingDirectory=$ZAPY_DIR" /etc/systemd/system/*.service 2>/dev/null | head -1 || true)"
    if [ -z "$unit" ]; then
      unit="$(grep -lxE "WorkingDirectory=($ZAPY_DIR|%h/${ZAPY_DIR#$HOME/})" "$HOME"/.config/systemd/user/*.service 2>/dev/null | head -1 || true)"
      [ -z "$unit" ] && [ -f "$HOME/.config/systemd/user/$(basename "$ZAPY_DIR").service" ] && unit="$HOME/.config/systemd/user/$(basename "$ZAPY_DIR").service"
      if [ -n "$unit" ] && [ -z "${ZAPY_SYSTEMCTL:-}" ]; then SC="systemctl --user"; fi
    fi
    ZAPY_SERVICE="$(basename "${unit:-zapy.service}" .service)"
  fi
fi
user_bus
# Confere o servico ANTES de mexer no codigo.
if ! unit_exists "$ZAPY_SERVICE"; then
  echo "[zapy] servico $ZAPY_SERVICE nao encontrado ($SC); nada foi alterado."
  echo "[zapy] servicos com zapy no nome: $(systemctl list-units --all --no-legend '*zapy*' 2>/dev/null | awk '{print $1}' | tr '\n' ' ')$(systemctl --user list-units --all --no-legend '*zapy*' 2>/dev/null | awk '{print $1" (--user)"}' | tr '\n' ' ')"
  exit 1
fi
echo "[zapy] servico: $ZAPY_SERVICE ($SC)"

TS="$(date +%Y%m%d-%H%M%S)"
mkdir -p ~/zapy-backups
OLDS="${OLD:0:7}"; OLDS="${OLDS:-semgit}"
BK=~/zapy-backups/zapy-$TS-$OLDS.tgz
# GNU tar sai com 1 quando um arquivo muda durante a leitura (sqlite do servico rodando);
# o backup continua valido para o resto, entao so 2+ e erro de verdade.
tar czf "$BK" --exclude=.venv --exclude=__pycache__ --exclude=bin . || [ $? -eq 1 ]
chmod 600 "$BK"
echo "[zapy] backup: $BK"

pip_install() { echo "[zapy] pip install"; .venv/bin/pip install -q -r requirements.txt; }

if [ -z "$OLD" ]; then
  echo "[zapy] instalacao sem git; convertendo para git em ${NEW:0:7}"
  git init -q
  git fetch -q "$TMPGIT" "$NEW"
  rm -rf "$TMPGIT"
  git reset -q "$NEW"
  git checkout -q -B main
  git checkout -q -- .
  pip_install
else
  git merge -q --ff-only "$NEW"
  git diff --quiet "$OLD" "$NEW" -- requirements.txt || pip_install
  git diff --quiet "$OLD" "$NEW" -- install.sh || echo "[zapy] AVISO: install.sh mudou; veja se precisa rodar de novo"
fi

echo "[zapy] $OLDS -> ${NEW:0:7}; reiniciando $ZAPY_SERVICE"
restarted=1
$SC restart "$ZAPY_SERVICE" || restarted=0
sleep 10
if [ "$restarted" = 0 ] || ! $SC is-active --quiet "$ZAPY_SERVICE"; then
  echo "[zapy] servico nao subiu; voltando ao estado anterior"
  if [ -z "$OLD" ]; then
    rm -rf .git
    tar xzf "$BK"
    pip_install
  else
    git reset -q --hard "$OLD"
    git diff --quiet "$OLD" "$NEW" -- requirements.txt || pip_install
  fi
  $SC restart "$ZAPY_SERVICE"
  sleep 5
  $SC is-active "$ZAPY_SERVICE" || true
  exit 1
fi

$SC status "$ZAPY_SERVICE" --no-pager -n 8 2>/dev/null | tail -8 || true
echo "[zapy] ok: $(git log --oneline -1)"
echo "[zapy] para voltar: cd $ZAPY_DIR && rm -rf .git && tar xzf $BK && $SC restart $ZAPY_SERVICE"
