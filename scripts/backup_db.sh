#!/usr/bin/env bash
# Backup diário do SQLite com rotação.
#
# Usa ``sqlite3 .backup`` — snapshot consistente mesmo com WAL ativo e
# o scheduler escrevendo ao mesmo tempo (diferente de cp, que pode
# copiar um arquivo no meio de um commit).
#
# Rotação: mantém os últimos $KEEP backups (default 14 = 2 semanas).
#
# Instalação no VPS: ver deploy/noviello-backup.timer (roda 03h diário).
# Restauração:
#   gunzip -k /opt/noviello-funil-saude/backups/noviello-YYYYMMDD-HHMMSS.db.gz
#   sudo systemctl stop noviello-followup.timer
#   cp <arquivo descompactado> /opt/noviello-funil-saude/data/noviello.db
#   sudo systemctl start noviello-followup.timer
set -euo pipefail

DB="${DB:-/opt/noviello-funil-saude/data/noviello.db}"
BACKUP_DIR="${BACKUP_DIR:-/opt/noviello-funil-saude/backups}"
KEEP="${KEEP:-14}"

if [ ! -f "$DB" ]; then
    echo "ERRO: DB não encontrado em $DB" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"
STAMP=$(date +%Y%m%d-%H%M%S)
OUT="$BACKUP_DIR/noviello-$STAMP.db"

sqlite3 "$DB" ".backup '$OUT'"
gzip "$OUT"

# Rotação: apaga tudo além dos $KEEP mais recentes.
ls -1t "$BACKUP_DIR"/noviello-*.db.gz 2>/dev/null \
    | tail -n +$((KEEP + 1)) \
    | xargs -r rm --

echo "backup ok: $OUT.gz ($(du -h "$OUT.gz" | cut -f1)) — $(ls "$BACKUP_DIR" | wc -l) backups mantidos"
