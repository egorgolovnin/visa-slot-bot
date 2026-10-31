#!/usr/bin/env python3
"""CYP Hunter v1.1: быстрый опрос публичного API VFS Global (Кипр).

Каждые INTERVAL секунд дёргает /appointment/centerwithearliestslot по всем
включённым миссиям и шлёт алерт в Telegram при появлении/смене даты слота.
Логин не нужен. Миссии: config/hunter.json (вкл/выкл через hunter_mission.sh).
"""
import json, os, time
from pathlib import Path
from curl_cffi import requests

ROOT = Path("/root/visa-slot-bot/monitor")
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
STATE_FILE = DATA / "hunter_state.json"
CFG_FILE = ROOT / "config" / "hunter.json"
ENV_FILE = ROOT / "config" / ".env"

COUNTRY = "cyp"
INTERVAL = int(os.environ.get("INTERVAL", "30"))
REQ_TIMEOUT = 15
URL = "https://lift-api.vfsglobal.com/appointment/centerwithearliestslot"
PROFILES = ("chrome", "chrome124")

# ---------- config ----------
def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env

def load_cfg():
    default = {"missions": {"aut": True, "hun": True, "svk": True, "fin": True,
                            "hrv": True, "grc": False, "ita": False, "che": False,
                            "cze": False, "pol": False, "mlt": False}}
    if CFG_FILE.exists():
        try:
            cfg = json.loads(CFG_FILE.read_text())
            if "missions" in cfg:
                return cfg
        except Exception:
            pass
    CFG_FILE.write_text(json.dumps(default, indent=1))
    return default

# ---------- proxy pool ----------
def _bot_proxy():
    for cand in ("/root/visa-slot-bot/.env", str(Path.home() / "visa-slot-bot" / ".env")):
        try:
            for line in open(cand):
                if line.startswith("PROXY_URL="):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if v:
                        return v
        except FileNotFoundError:
            continue
    return None

def proxy_pool():
    pool = []
    bp = _bot_proxy()
    if bp:
        pool.append(bp)
    px_file = ROOT / "config" / "proxies.txt"
    if px_file.exists():
        for line in px_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":", 3)
            if len(parts) == 4:
                h, p, u, pw = parts
                px = f"http://{u}:{pw}@{h}:{p}"
                if px not in pool:
                    pool.append(px)
    env = load_env()
    for cand in (env.get("VFS_PROXY"), os.environ.get("PROXY_URL")):
        if cand and cand not in pool:
            pool.append(cand)
    return pool

# ---------- telegram ----------
def tg_send(text, env):
    tok, chat = env.get("TG_TOKEN"), env.get("OWNER_CHAT_ID")
    if not tok or not chat:
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": text, "parse_mode": "HTML"},
                          timeout=15)
        return r.status_code == 200
    except Exception:
        return False

# ---------- state ----------
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"slots": {}, "alerted": {}, "beat": 0, "total_alerts": 0}

def save_state(st):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    tmp.replace(STATE_FILE)

# ---------- poll ----------
def poll(mission, proxies):
    hdrs = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": "https://visa.vfsglobal.com",
        "referer": "https://visa.vfsglobal.com/",
        "accept-language": "en-US,en;q=0.9",
        "route": f"{COUNTRY}/en/{mission}",
    }
    payload = {"missionCode": mission, "countryCode": COUNTRY, "cultureCode": "en-US"}
    last_err = None
    for px in (list(proxies) if proxies else [None]):
        for profile in PROFILES:
            try:
                r = requests.post(URL, headers=hdrs, json=payload,
                                  proxies={"http": px, "https": px} if px else None,
                                  impersonate=profile, timeout=REQ_TIMEOUT)
                if r.status_code == 200 and (r.text or "").lstrip().startswith("{"):
                    return r.json(), None
                last_err = f"status {r.status_code}"
            except Exception as e:
                last_err = str(e)[:70]
    return None, last_err

def extract(data):
    out = []
    for vac in (data or {}).get("vacList") or []:
        centre = (vac.get("vacName") or vac.get("vacCode") or "?").strip()
        for g in vac.get("visaGroupList") or []:
            name = (g.get("displayName") or g.get("code") or "?").strip()
            raw = (g.get("earliestAvailableDate") or "").strip()
            out.append((centre, name, raw))
    return out

def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)

# ---------- main ----------
def main():
    env = load_env()
    cfg = load_cfg()
    st = load_state()
    px = proxy_pool()
    log(f"hunter v1.1 start | interval={INTERVAL}s | proxies={len(px)} | "
        f"tg={'ok' if env.get('TG_TOKEN') and env.get('OWNER_CHAT_ID') else 'НЕ НАСТРОЕН'}")
    while True:
        missions = [m for m, on in cfg["missions"].items() if on]
        cycle_slots = 0
        for m in missions:
            data, err = poll(m, px)
            if err:
                log(f"{m}: ERR {err}")
                continue
            found = extract(data)
            with_dates = [f for f in found if f[2]]
            cycle_slots += len(with_dates)
            log(f"{m}: ok, slots={len(with_dates)}")
            for centre, cat, raw in found:
                key = f"{m}|{centre}|{cat}"
                prev = st["slots"].get(key)
                if prev != raw:
                    st["slots"][key] = raw
                    alert_key = f"{key}|{raw}"
                    if alert_key not in st["alerted"]:
                        st["alerted"][alert_key] = time.time()
                        st["total_alerts"] = st.get("total_alerts", 0) + 1
                        txt = (f"<b>SLOT {m.upper()}</b> {cat}\n"
                               f"{centre}\nДата: <b>{raw}</b>")
                        ok = tg_send(txt, env)
                        log(f"ALERT {m} {cat} {raw} tg={'ok' if ok else 'FAIL'}")
        # heartbeat + очистка alerted старше 24ч
        st["beat"] = st.get("beat", 0) + 1
        now = time.time()
        st["alerted"] = {k: v for k, v in st["alerted"].items() if now - v < 24 * 3600}
        save_state(st)
        if st["beat"] % 20 == 0:
            log(f"heartbeat #{st['beat']}: слотов с датами за цикл={cycle_slots}, "
                f"алертов всего={st['total_alerts']}")
        time.sleep(INTERVAL)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
