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

if [ -z "${ZAPY_SERVICE:-}" ]; then
  unit="$(grep -lx "WorkingDirectory=$ZAPY_DIR" /etc/systemd/system/*.service 2>/dev/null | head -1 || true)"
  ZAPY_SERVICE="$(basename "${unit:-zapy.service}" .service)"
fi

if [ -f "$SRC" ]; then
  SRC="$(realpath "$SRC")"
  git bundle verify -q "$SRC" >/dev/null
fi
cd "$ZAPY_DIR"
echo "[zapy] pasta: $ZAPY_DIR  servico: $ZAPY_SERVICE  origem: $SRC ($REF)"

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

TS="$(date +%Y%m%d-%H%M%S)"
mkdir -p ~/zapy-backups
OLDS="${OLD:0:7}"; OLDS="${OLDS:-semgit}"
BK=~/zapy-backups/zapy-$TS-$OLDS.tgz
tar czf "$BK" --exclude=.venv --exclude=__pycache__ --exclude=bin .
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
$SC restart "$ZAPY_SERVICE"
sleep 10
if ! $SC is-active --quiet "$ZAPY_SERVICE"; then
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
