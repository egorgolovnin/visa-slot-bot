import pathlib, sys
ok = []
def rep(path, old, new, count=1):
    p = pathlib.Path(path); s = p.read_text()
    if new.split("\n")[0] in s and old not in s:
        ok.append(f"SKIP {path} (уже пропатчен)"); return
    assert old in s, f"NOT FOUND in {path}: {old[:70]}"
    p.write_text(s.replace(old, new, count)); ok.append(f"OK   {path}")

# ---------- auto_login.py ----------
rep("auto_login.py",
    "import argparse, asyncio, base64, json, random, re, time, uuid, sys",
    "import argparse, asyncio, base64, json, os, random, re, time, uuid, sys")
rep("auto_login.py",
    'LOGIN_PAGE = "https://visa.vfsglobal.com/uzb/en/lva/login"',
    'VFS_COUNTRY = os.environ.get("VFS_COUNTRY", "cyp")\n'
    'VFS_MISSION = os.environ.get("VFS_MISSION", "aut")\n'
    'VFS_CENTER  = os.environ.get("VFS_CENTER", "NIC")\n'
    'ROUTE       = f"{VFS_COUNTRY}/en/{VFS_MISSION}"\n'
    'LOGIN_PAGE  = f"https://visa.vfsglobal.com/{ROUTE}/login"\n'
    'TURNSTILE_SITEKEY = os.environ.get("TURNSTILE_SITEKEY", "0x4AAAAAABhlz7Ei4byodYjs")')
rep("auto_login.py", '_pr = await _pc.get("https://visa.vfsglobal.com/uzb/en/lva/login", timeout=12)',
    '_pr = await _pc.get(LOGIN_PAGE, timeout=12)')
rep("auto_login.py", 'r1 = await s.get("https://visa.vfsglobal.com/uzb/en/lva/login",',
    'r1 = await s.get(LOGIN_PAGE,')
rep("auto_login.py", '"route": "uzb/en/lva",', '"route": ROUTE,')
rep("auto_login.py", 'r2 = await s.get(f"{LIFT_API}/configuration/fields/lva/uzb",',
    'r2 = await s.get(f"{LIFT_API}/configuration/fields/{VFS_MISSION}/{VFS_COUNTRY}",')
rep("auto_login.py", '"missioncode": "lva", "countrycode": "uzb", "languageCode": "en-US",',
    '"missioncode": VFS_MISSION, "countrycode": VFS_COUNTRY, "languageCode": "en-US",')
rep("auto_login.py", '"countryCode": "uzb", "missionCode": "lva", "centerCode": "TAS",',
    '"countryCode": VFS_COUNTRY, "missionCode": VFS_MISSION, "centerCode": VFS_CENTER,')

# ---------- datewatch.py ----------
rep("datewatch.py",
    'WORK={"LSHMEDCL":"Work UZB","LNGWORK":"Cargo UZB","LNGRSDTJK":"Work TJK","LNGWORKTJK":"Cargo TJK"}',
    'WORK={"LSHMEDCL":"Work UZB","LNGWORK":"Cargo UZB","LNGRSDTJK":"Work TJK","LNGWORKTJK":"Cargo TJK"}\n'
    'try:\n'
    '    _cfg=json.load(open(ROOT+"/config/datewatch.json"))\n'
    '    WORK=_cfg.get("categories",WORK) or WORK\n'
    '    CTRL=_cfg.get("control","LNGSTUD")\n'
    '    TITLE=_cfg.get("title","LVA")\n'
    'except Exception:\n'
    '    CTRL="LNGSTUD"; TITLE="LVA"')
p = pathlib.Path("datewatch.py"); s = p.read_text()
if '"LNGSTUD"' in s:
    s = s.replace('"LNGSTUD"', 'CTRL').replace('(LVA)', '({TITLE})')
    p.write_text(s); ok.append("OK   datewatch.py (LNGSTUD→CTRL)")

# ---------- multi_auto_run.py ----------
rep("multi_auto_run.py",
    "from auto_login import auto_login as do_login, cs_now, DATA, ROOT, LIFT_API",
    "from auto_login import auto_login as do_login, cs_now, DATA, ROOT, LIFT_API, VFS_COUNTRY, VFS_MISSION, VFS_CENTER")
rep("multi_auto_run.py", 'CONTROL_CAT = "LNGSTUD"',
    'CONTROL_CAT = os.environ.get("CONTROL_CAT", "LNGSTUD")')
rep("multi_auto_run.py",
    '''CATEGORIES = {
    "LNGSTUD":    "Студенты UZB",
    "LSHMEDCL":   "Work UZB/TKM",
    "LNGWORK":    "Cargo drivers UZB/TKM",
    "LNGRSDTJK":  "Work TJK",
    "LNGWORKTJK": "Cargo drivers TJK",
}''',
    '''CATEGORIES = {
    "LNGSTUD":    "Студенты UZB",
    "LSHMEDCL":   "Work UZB/TKM",
    "LNGWORK":    "Cargo drivers UZB/TKM",
    "LNGRSDTJK":  "Work TJK",
    "LNGWORKTJK": "Cargo drivers TJK",
}
try:
    _cfg = json.load(open(ROOT / "config" / "datewatch.json"))
    if _cfg.get("categories"):
        CATEGORIES = dict(_cfg["categories"])
        if _cfg.get("control"):
            CONTROL_CAT = _cfg["control"]
except Exception:
    pass''')
rep("multi_auto_run.py",
    'body = {"countryCode": "uzb", "missionCode": "lva", "vacCode": "TAS",',
    'body = {"countryCode": VFS_COUNTRY, "missionCode": VFS_MISSION, "vacCode": VFS_CENTER,')
rep("multi_auto_run.py", '"route": "uzb/en/lva",', '"route": f"{VFS_COUNTRY}/en/{VFS_MISSION}",')

print("\n".join(ok))
