"""Shell runner for the non-VFS systems (no login, no VFS rate-limit — safe to run repeatedly):

    docker compose exec bot python -m app._rk_run        # RK-Termin: Germany + NL/BE/DK/NO/IS/LU
    docker compose exec bot python -m app._rk_run grc    # SuperSaaS: Greece (+Malta), if creds set

Prints the full result: captcha solved?, free days, earliest date, times, or WHY it failed.
"""
import asyncio
import sys
import time

from . import config, supersaas
from .rktermin import RkTerminClient
from .supersaas import SuperSaasClient
from .vfs import Blocked


async def run_rk(proxy: str | None) -> None:
    rk = RkTerminClient(proxy)
    print(f"[rk] URL: {rk.page_url()}")
    print(f"[rk] category_id={rk.category_id} proxy={'yes' if proxy else 'no'}")
    t0 = time.time()
    try:
        slots, stamp = await rk.fetch()
    except Blocked as e:
        print(f"[rk] ❌ BLOCKED ({time.time()-t0:.1f}s): {e}")
        return
    except Exception as e:
        print(f"[rk] ❌ ERROR ({time.time()-t0:.1f}s): {type(e).__name__}: {e}")
        return
    dt = time.time() - t0
    s = slots[0] if slots else None
    print(f"[rk] ✅ ok in {dt:.1f}s  — free days: {rk.last_free_days}")
    if s and s.earliest:
        print(f"[rk] ближайшая дата: {s.earliest.isoformat()}")
        if rk.last_times:
            for d, times in sorted(rk.last_times.items()):
                print(f"       {d.isoformat()}: {', '.join(times) or '(времена не прочитаны)'}")
    else:
        print("[rk] свободных дат нет (страница отдалась, капча прошла — но слотов 0)")
    if rk.form_report:
        print(f"[rk] форма записи:\n{rk.form_report}")


async def run_ss(proxy: str | None) -> None:
    if not supersaas.enabled():
        print("[ss] SuperSaaS выключен — не заданы SUPERSAAS_EMAIL / SUPERSAAS_PASSWORD")
        return
    ss = SuperSaasClient(proxy)
    t0 = time.time()
    try:
        slots, stamp = await ss.fetch()
    except Blocked as e:
        print(f"[ss] ❌ BLOCKED ({time.time()-t0:.1f}s): {e}")
        return
    except Exception as e:
        print(f"[ss] ❌ ERROR ({time.time()-t0:.1f}s): {type(e).__name__}: {e}")
        return
    dt = time.time() - t0
    s = slots[0] if slots else None
    print(f"[ss] ✅ ok in {dt:.1f}s")
    if s and s.earliest:
        print(f"[ss] ближайшая дата: {s.earliest.isoformat()}")
    else:
        print("[ss] свободных дат нет")


async def main() -> None:
    target = (sys.argv[1] if len(sys.argv) > 1 else "deu").strip().lower()
    settings = config.load()
    if target in ("grc", "ss", "greece", "greek"):
        await run_ss(settings.proxy_url)
    else:
        await run_rk(settings.proxy_url)


if __name__ == "__main__":
    asyncio.run(main())
