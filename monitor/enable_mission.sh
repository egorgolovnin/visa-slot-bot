#!/usr/bin/env bash
# Переключить монитор на другую миссию VFS: bash enable_mission.sh hrv [NIC]
set -e
cd "$(dirname "$0")"
m="$1"; c="${2:-NIC}"
[ -z "$m" ] && { echo "usage: bash enable_mission.sh <mission> [center]"; exit 1; }
touch config/.env
sed -i "s/^VFS_MISSION=.*/VFS_MISSION=$m/" config/.env
sed -i "s/^VFS_CENTER=.*/VFS_CENTER=$c/" config/.env
grep -q "^VFS_MISSION=" config/.env || echo "VFS_MISSION=$m" >> config/.env
grep -q "^VFS_CENTER=" config/.env || echo "VFS_CENTER=$c" >> config/.env
echo "✅ миссия $m (центр $c) включена"
echo "   перезапусти: systemctl restart datewatch  (или Ctrl+C и заново python3 datewatch.py)"
