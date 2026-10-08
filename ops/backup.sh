#!/usr/bin/env bash
# Daily Postgres backup → gzip → Telegram admin chat. Keeps the last 14 locally.
# Run from cron on the VPS. Safe to run by hand too.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# load BOT_TOKEN / ADMIN_CHAT_ID / ADMIN_IDS from .env
set -a
# shellcheck disable=SC1091
source .env
set +a

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="${BACKUP_DIR:-/root/backups}"
mkdir -p "$BACKUP_DIR"
FILE="$BACKUP_DIR/visabot-$STAMP.sql.gz"

# dump inside the db container (no client needed on the host)
docker compose exec -T db pg_dump -U bot -d visabot | gzip -9 > "$FILE"

# retention: keep the 14 newest dumps, delete the rest
ls -1t "$BACKUP_DIR"/visabot-*.sql.gz 2>/dev/null | tail -n +15 | xargs -r rm -f

# where to send: group chat if set, else the first admin DM
CHAT="${ADMIN_CHAT_ID:-}"
[ -z "$CHAT" ] && CHAT="${ADMIN_IDS%%,*}"

SIZE="$(du -h "$FILE" | cut -f1)"
BYTES="$(stat -c%s "$FILE")"

# Telegram bot API caps sendDocument at 50 MB. Warn instead of silently failing.
if [ "$BYTES" -gt 49000000 ]; then
  curl -sS -F chat_id="$CHAT" \
    -F text="⚠️ Бэкал базы $STAMP весит $SIZE — больше лимита Telegram (50 МБ). Файл сохранён локально: $FILE" \
    "https://api.telegram.org/bot$BOT_TOKEN/sendMessage" >/dev/null || true
  echo "backup too big for telegram, kept locally: $FILE"
  exit 0
fi

if curl -sS --max-time 120 \
    -F chat_id="$CHAT" \
    -F document=@"$FILE" \
    -F caption="🗄 Бэкап базы $STAMP ($SIZE)" \
    "https://api.telegram.org/bot$BOT_TOKEN/sendDocument" >/dev/null; then
  echo "backup sent to telegram: $FILE"
else
  echo "telegram send failed; local copy kept: $FILE" >&2
  exit 1
fi
