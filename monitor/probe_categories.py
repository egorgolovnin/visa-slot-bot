import json, asyncio
from pathlib import Path
from curl_cffi.requests import AsyncSession

COUNTRY = "cyp"
MISSIONS = ["aut", "hun", "svk", "fin", "hrv", "grc", "ita", "che", "cze", "pol", "mlt", "est", "svn", "ltu"]
line = [l for l in Path("config/proxies.txt").read_text().splitlines() if l.strip()][0]
h, p, u, pw = line.split(":", 3)
proxy = f"http://{u}:{pw}@{h}:{p}"

def walk(x, key, out):
    if isinstance(x, dict):
        for k, v in x.items():
            if k.lower() == key and isinstance(v, str):
                out.add(v)
            walk(v, key, out)
    elif isinstance(x, list):
        for v in x:
            walk(v, key, out)

async def main():
    res = {}
    async with AsyncSession(proxies={"http": proxy, "https": proxy}, timeout=30) as s:
        for m in MISSIONS:
            try:
                r = await s.get(f"https://lift-api.vfsglobal.com/configuration/fields/{m}/{COUNTRY}",
                                headers={"accept": "application/json, text/plain, */*",
                                         "origin": "https://visa.vfsglobal.com",
                                         "referer": "https://visa.vfsglobal.com/",
                                         "route": f"{COUNTRY}/en/{m}"},
                                impersonate="chrome")
                d = r.json()
            except Exception as e:
                print(f"{m}: ERR {type(e).__name__} {str(e)[:80]}"); continue
            cats, centers, names = set(), set(), {}
            walk(d, "visacategorycode", cats)
            walk(d, "centercode", centers)
            # ищем пары код+название в опциях категорий
            def walk_opts(x):
                if isinstance(x, dict):
                    code = x.get("code") or x.get("value")
                    name = x.get("name") or x.get("label") or x.get("text")
                    if isinstance(code, str) and isinstance(name, str) and len(code) < 20:
                        names[code] = name
                    for v in x.values(): walk_opts(v)
                elif isinstance(x, list):
                    for v in x: walk_opts(v)
            walk_opts(d)
            print(f"{m}: cats={sorted(cats)} centers={sorted(centers)}")
            res[m] = {"categories": dict(names), "centers": sorted(centers)}
    Path("config/probe_fields.json").write_text(json.dumps(res, indent=2, ensure_ascii=False))
    print("\n→ сохранено в config/probe_fields.json")

asyncio.run(main())
