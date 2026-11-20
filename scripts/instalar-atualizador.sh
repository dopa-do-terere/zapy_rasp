#!/usr/bin/env bash
# Libera o botao "Atualizar agora" (Configuracoes do zapy) sem dar sudo amplo ao usuario.
# Rode UMA vez no Pi, com a senha do sudo:
#   sudo bash ~/zapy_rasp/scripts/instalar-atualizador.sh
#
# Cria /usr/local/sbin/zapy-update (root, sem argumentos: roda o atualizar-zapy.sh como o
# usuario do zapy, fora do servico) e /etc/sudoers.d/zapy-update permitindo SEM senha so:
# esse comando e reiniciar/consultar o servico do zapy.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "[zapy] rode com sudo: sudo bash $0"; exit 1; }

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="${ZAPY_USER:-$(stat -c %U "$DIR")}"
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
SERVICE="${ZAPY_SERVICE:-$(basename "$(grep -lx "WorkingDirectory=$DIR" /etc/systemd/system/*.service 2>/dev/null | head -1 || true)" .service)}"
SERVICE="${SERVICE:-zapy}"
SC="$(command -v systemctl)"
case "$USER_NAME$DIR$HOME_DIR$SERVICE" in *"'"*|*" "*) echo "[zapy] caminho com aspas ou espaco nao suportado"; exit 1;; esac

cat > /usr/local/sbin/zapy-update <<EOF
#!/bin/bash
# Gerado por scripts/instalar-atualizador.sh — nao editar.
set -e
LOG='$HOME_DIR/zapy-backups/update-last.log'
exec systemd-run --unit "zapy-update-\$(date +%s)" --collect --quiet --uid='$USER_NAME' \\
  --setenv=HOME='$HOME_DIR' --setenv=ZAPY_DIR='$DIR' --setenv=ZAPY_SERVICE='$SERVICE' --working-directory='$DIR' \\
  /bin/bash -c "bash '$DIR/scripts/atualizar-zapy.sh' >> '\$LOG' 2>&1; echo FIM exit=\\\$? >> '\$LOG'"
EOF
chown root:root /usr/local/sbin/zapy-update
chmod 755 /usr/local/sbin/zapy-update

TMP="$(mktemp)"
cat > "$TMP" <<EOF
# Gerado por scripts/instalar-atualizador.sh (botao Atualizar do zapy).
$USER_NAME ALL=(root) NOPASSWD: /usr/local/sbin/zapy-update, $SC restart $SERVICE, $SC is-active $SERVICE, $SC is-active --quiet $SERVICE, $SC status $SERVICE --no-pager -n 8
EOF
if visudo -cf "$TMP" >/dev/null; then
  install -m 440 -o root -g root "$TMP" /etc/sudoers.d/zapy-update
  rm -f "$TMP"
else
  rm -f "$TMP"; echo "[zapy] regra do sudo invalida; nada foi alterado"; exit 1
fi
echo "[zapy] botao Atualizar liberado para $USER_NAME (servico $SERVICE, pasta $DIR)."
