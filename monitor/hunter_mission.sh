#!/usr/bin/env bash
# Включить/выключить миссию охотника: bash hunter_mission.sh hrv on|off
set -e
cd "$(dirname "$0")"
m="$1"; on="${2:-on}"
[ -z "$m" ] && { echo "usage: bash hunter_mission.sh <mission> on|off"; exit 1; }
.venv/bin/python - "$m" "$on" <<'PYEOF'
import json, sys
from pathlib import Path
cfg_p = Path("config/hunter.json")
cfg = json.loads(cfg_p.read_text()) if cfg_p.exists() else {"missions": {}}
cfg["missions"][sys.argv[1]] = sys.argv[2] == "on"
cfg_p.write_text(json.dumps(cfg, indent=1))
print("missions:", {k: v for k, v in sorted(cfg["missions"].items())})
PYEOF
echo "перезапусти охотника: systemctl restart cyp-hunter"
