import json, time
from curl_cffi import requests

COUNTRY = "cyp"
MISSIONS = ["aut", "hun", "svk", "fin", "hrv", "grc", "ita", "che", "cze", "pol", "mlt"]

line = [l.strip() for l in open("config/proxies.txt") if l.strip()][0]
h, p, u, pw = line.split(":", 3)
proxy = f"http://{u}:{pw}@{h}:{p}"
proxies = {"http": proxy, "https": proxy}

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
            if groups:
                print("   group[0]:", json.dumps(groups[0], ensure_ascii=False)[:400])
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
