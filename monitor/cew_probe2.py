import json, time, os, re
from curl_cffi import requests

COUNTRY = "cyp"
MISSIONS = ["hrv", "aut", "hun", "svk", "fin", "grc", "ita", "che", "cze", "pol", "mlt"]

# прокси из .env бота (PROXY_URL) - тот, что реально работает
proxy = None
for cand in ("/root/visa-slot-bot/.env", os.path.expanduser("~/visa-slot-bot/.env")):
    try:
        for line in open(cand):
            if line.startswith("PROXY_URL="):
                proxy = line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        continue
    if proxy:
        break
print("proxy:", (proxy.split("@")[-1] if proxy else "НЕ НАЙДЕН"))
proxies = {"http": proxy, "https": proxy} if proxy else None

H = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json",
    "origin": "https://visa.vfsglobal.com",
    "referer": "https://visa.vfsglobal.com/",
}

URL = "https://lift-api.vfsglobal.com/appointment/centerwithearliestslot"
raw_all = {}
for m in MISSIONS:
    hdrs = dict(H)
    hdrs["route"] = f"{COUNTRY}/en/{m}"
    ok = False
    for attempt in range(3):
        try:
            r = requests.post(URL, headers=hdrs, json={"missionCode": m},
                              proxies=proxies, impersonate="chrome", timeout=40)
            d = r.json()
            vacs = d.get("vacList") or []
            groups = []
            for vac in vacs:
                groups.extend(vac.get("visaGroupList") or [])
            print(f"{m}: status={r.status_code} vacs={len(vacs)} groups={len(groups)}")
            for g in groups[:5]:
                print("   GROUP:", json.dumps(g, ensure_ascii=False)[:500])
            raw_all[m] = d
            ok = True
            break
        except Exception as e:
            print(f"{m}: attempt {attempt+1} ERR {str(e)[:70]}")
            time.sleep(2)
    if not ok:
        raw_all[m] = None
    time.sleep(1)

json.dump(raw_all, open("config/cew_raw.json", "w"), indent=1, ensure_ascii=False)
print("\n-> config/cew_raw.json")
