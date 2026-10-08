#!/usr/bin/env bash
# One-time server hardening for the VPS. Run as root: bash ops/harden.sh
# Idempotent — safe to run again.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> installing firewall / fail2ban / auto-updates"
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y ufw fail2ban unattended-upgrades

echo "==> firewall: deny all inbound except SSH (22) and the bot web port (8080)"
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 8080/tcp
ufw --force enable
# NOTE: Docker publishes ports straight into iptables and bypasses ufw, so the
# bot's 8080 is reachable regardless. Postgres (5432) and FlareSolverr (8191) are
# NOT published to the host in docker-compose.yml, so they stay internal. ufw still
# protects every non-docker service on the box (e.g. the VPN).

echo "==> fail2ban: ban SSH brute-force"
systemctl enable --now fail2ban

echo "==> unattended security upgrades"
cat >/etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF
systemctl enable --now unattended-upgrades || true

echo "==> docker log rotation (stop logs filling the disk)"
mkdir -p /etc/docker
if [ ! -f /etc/docker/daemon.json ]; then
  cat >/etc/docker/daemon.json <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
JSON
  systemctl restart docker || true
else
  echo "    /etc/docker/daemon.json already exists — left as is, add log-opts manually if missing"
fi

echo "==> lock down secrets"
chmod 600 "$PROJECT_DIR/.env" 2>/dev/null || true

echo
echo "DONE. Check:  ufw status verbose  |  fail2ban-client status sshd"
echo "Reminder: rotate the secrets that were committed to git (see ops/README.md)."
