"""Order funnel: where people drop off between /order and payment."""
from __future__ import annotations

import json

ORDER = ["start", "destination", "window", "count", "full_name", "gender", "birth", "nationality", "passport",
         "passport_exp", "reference", "passport_scan", "residence_exp", "phone", "confirm", "submitted"]
LABELS = {"start": "Начали /order", "destination": "Выбрали страну", "window": "Даты", "count": "Сколько человек",
          "full_name": "Имя", "gender": "Пол", "birth": "Дата рождения", "nationality": "Гражданство",
          "passport": "Номер паспорта", "passport_exp": "Срок паспорта", "reference": "France-Visas",
          "passport_scan": "Скан паспорта", "residence_exp": "ВНЖ", "phone": "Телефон", "confirm": "Итоговый экран",
          "submitted": "Отправили заявку", "fee_ok": "Сбор оплачен / не нужен", "booked": "Забронировано",
          "paid": "Оплатили сервис"}


async def build(db, days: int) -> dict:
    steps = {r["step"]: r["users"] for r in await db.funnel(days)}
    o = await db.funnel_orders(days)
    rows = [{"key": k, "label": LABELS[k], "users": steps[k]} for k in ORDER if k in steps]
    if o and o["submitted"]:
        rows += [{"key": k, "label": LABELS[k], "users": o[k]} for k in ("fee_ok", "booked", "paid")]
    top = rows[0]["users"] if rows else 0
    for i, r in enumerate(rows):
        r["pct"] = round(100 * r["users"] / top) if top else 0
        nxt = rows[i + 1]["users"] if i + 1 < len(rows) else None
        # people who reached this step but not the next one stopped here
        r["drop"] = r["users"] - nxt if nxt is not None and r["users"] > nxt else 0
        r["note"] = {"fee_ok": "ждут слот", "booked": "ждут оплаты", "submitted": "ждут оплаты сбора"}.get(r["key"], "ушли здесь")
    stuck: dict[str, int] = {}
    for f in await db.abandoned_forms(min_minutes=30):
        step = f["state"].split(":")[-1]
        stuck[step] = stuck.get(step, 0) + 1
    return {"days": days, "rows": rows, "stuck": [{"step": LABELS.get(k, k), "n": v} for k, v in
                                                  sorted(stuck.items(), key=lambda kv: -kv[1])]}


def text(f: dict) -> str:
    if not f["rows"]:
        return f"🧭 <b>Воронка за {f['days']} дн</b>\nПока никто не начинал заявку."
    lines = [f"🧭 <b>Воронка за {f['days']} дн</b> (уникальные люди)"]
    for r in f["rows"]:
        drop = f"  <i>{r['drop']} {r['note']}</i>" if r["drop"] else ""
        lines.append(f"{r['users']:>4} · {r['pct']:>3}% · {r['label']}{drop}")
    if f["stuck"]:
        lines.append("\n<b>Сейчас застряли в анкете</b> (30+ мин): " + ", ".join(f"{s['step']} — {s['n']}" for s in f["stuck"]))
    worst = max((r for r in f["rows"] if r["drop"] and r["key"] not in ("submitted", "fee_ok", "booked")),
                key=lambda r: r["drop"], default=None)
    if worst:
        lines.append(f"\nБольше всего бросают анкету на шаге «{worst['label']}» (−{worst['drop']}).")
    return "\n".join(lines)
