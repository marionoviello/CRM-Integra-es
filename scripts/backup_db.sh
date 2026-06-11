#!/usr/bin/env bash
# Backup diário do SQLite com rotação e verificação de integridade.
#
# Usa ``sqlite3 .backup`` — snapshot consistente mesmo com WAL ativo e
# o scheduler escrevendo ao mesmo tempo (diferente de cp, que pode
# copiar um arquivo no meio de um commit).
#
# Rotação: mantém os últimos $KEEP backups (default 14 = 2 semanas).
# Integridade (auditoria 2026-06-11): cada backup passa por
# PRAGMA integrity_check + gzip -t ANTES de entrar na rotação — um
# backup corrompido aborta o script (systemd marca failed) sem apagar
# os backups bons.
#
# Instalação no VPS: ver deploy/noviello-backup.timer (roda 03h diário).
#
# RESTAURAÇÃO (auditoria 2026-06-11 — o procedimento antigo corrompia
# o banco: não parava o FastAPI e deixava -wal/-shm órfãos do banco
# antigo serem aplicados sobre o restaurado):
#
#   1. PARAR TUDO que escreve no banco (timer E serviço web):
#      sudo systemctl stop noviello-followup.timer noviello-funil.service
#   2. Remover o banco atual E os arquivos WAL/SHM:
#      rm -f /opt/noviello-funil-saude/data/noviello.db \
#            /opt/noviello-funil-saude/data/noviello.db-wal \
#            /opt/noviello-funil-saude/data/noviello.db-shm
#   3. Restaurar o backup:
#      gunzip -kc /opt/noviello-funil-saude/backups/noviello-XXXX.db.gz \
#          > /opt/noviello-funil-saude/data/noviello.db
#      chown noviello:noviello /opt/noviello-funil-saude/data/noviello.db
#   4. Religar:
#      sudo systemctl start noviello-funil.service noviello-followup.timer
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

# Verificação de integridade do snapshot ANTES de comprimir/rotacionar.
CHECK=$(sqlite3 "$OUT" "PRAGMA integrity_check;" | head -1)
if [ "$CHECK" != "ok" ]; then
    echo "ERRO: integrity_check falhou no snapshot: $CHECK" >&2
    rm -f "$OUT"
    exit 1
fi

gzip "$OUT"

# Confere que o .gz não saiu corrompido (disco cheio, I/O error).
if ! gzip -t "$OUT.gz"; then
    echo "ERRO: gzip -t falhou em $OUT.gz — backup descartado, rotação abortada" >&2
    rm -f "$OUT.gz"
    exit 1
fi

# Rotação: apaga tudo além dos $KEEP mais recentes (só roda se o backup
# de hoje passou nas verificações acima).
ls -1t "$BACKUP_DIR"/noviello-*.db.gz 2>/dev/null \
    | tail -n +$((KEEP + 1)) \
    | xargs -r rm --

echo "backup ok: $OUT.gz ($(du -h "$OUT.gz" | cut -f1)) — $(ls "$BACKUP_DIR" | wc -l) backups mantidos"
