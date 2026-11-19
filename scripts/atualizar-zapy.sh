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
# Variaveis opcionais:
#   ZAPY_DIR=~/zapy   ZAPY_SERVICE=zapy   ZAPY_SYSTEMCTL="sudo systemctl"
#
# Gerar o bundle no Mac:  git -C zapy_rasp bundle create zapy.bundle main
#
# Faz: backup (tar sem .venv) em ~/zapy-backups, fast-forward para o ref,
# pip install so se requirements.txt mudou, restart do servico e, se o servico
# nao subir, volta sozinho para o commit anterior.
set -euo pipefail

SRC="${1:-https://github.com/dopa-do-terere/zapy_rasp.git}"
REF="${2:-main}"
ZAPY_DIR="${ZAPY_DIR:-$HOME/zapy}"
ZAPY_SERVICE="${ZAPY_SERVICE:-zapy}"
SC="${ZAPY_SYSTEMCTL:-sudo systemctl}"

cd "$ZAPY_DIR"
if [ -f "$SRC" ]; then
  SRC="$(realpath "$SRC")"
  git bundle verify -q "$SRC" >/dev/null
fi
echo "[zapy] origem: $SRC ($REF)"

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "[zapy] ha arquivos versionados alterados; nada foi feito:"
  git status --short --untracked-files=no
  exit 1
fi

OLD="$(git rev-parse HEAD)"
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

TS="$(date +%Y%m%d-%H%M%S)"
mkdir -p ~/zapy-backups
BK=~/zapy-backups/zapy-$TS-${OLD:0:7}.tgz
tar czf "$BK" --exclude=.venv --exclude=__pycache__ --exclude=bin .
chmod 600 "$BK"
echo "[zapy] backup: $BK"

pip_if_needed() {
  if ! git diff --quiet "$1" "$2" -- requirements.txt; then
    echo "[zapy] requirements.txt mudou; pip install"
    .venv/bin/pip install -q -r requirements.txt
  fi
}

git merge -q --ff-only "$NEW"
pip_if_needed "$OLD" "$NEW"
git diff --quiet "$OLD" "$NEW" -- install.sh || echo "[zapy] AVISO: install.sh mudou; veja se precisa rodar de novo"

echo "[zapy] ${OLD:0:7} -> ${NEW:0:7}; reiniciando $ZAPY_SERVICE"
$SC restart "$ZAPY_SERVICE"
sleep 10
if ! $SC is-active --quiet "$ZAPY_SERVICE"; then
  echo "[zapy] servico nao subiu; voltando para ${OLD:0:7}"
  git reset -q --hard "$OLD"
  pip_if_needed "$NEW" "$OLD"
  $SC restart "$ZAPY_SERVICE"
  sleep 5
  $SC is-active "$ZAPY_SERVICE" || true
  exit 1
fi

$SC status "$ZAPY_SERVICE" --no-pager -n 8 2>/dev/null | tail -8 || true
echo "[zapy] ok: $(git log --oneline -1)"
echo "[zapy] para voltar: cd $ZAPY_DIR && git reset --hard ${OLD:0:7} && $SC restart $ZAPY_SERVICE"
