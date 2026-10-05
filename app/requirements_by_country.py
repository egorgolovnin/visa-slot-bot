"""What each submission system needs to book a slot, and what it charges at booking time.

fee_at_booking: EUR the booking system charges by card when the slot is taken (we prepay it with the
client's money, so the client pays it to us first). 0 = nothing at booking (fee, if any, is paid at the centre).
Sources: VFS Cyprus country pages (Hungary 22.80, Austria 25.27), the reseller chat (prepaid at booking),
VFS Finland page (paid at the centre). Slovakia is not confirmed — adjust with /fee svk <amount>.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Requirements:
    fee_at_booking: float
    gender: bool            # booking form asks for gender
    passport_scan: bool     # booking form requires the passport bio page upload
    system: str
    phone: bool = True      # booking form requires a phone number (VFS does)


VFS = "VFS Global"
REQUIREMENTS: dict[str, Requirements] = {
    "hun": Requirements(22.80, gender=True, passport_scan=True, system=VFS),
    "aut": Requirements(25.27, gender=True, passport_scan=True, system=VFS),
    "svk": Requirements(40.00, gender=True, passport_scan=False, system=VFS),
    "fin": Requirements(0.0, gender=True, passport_scan=False, system=VFS),
    "hrv": Requirements(0.0, gender=True, passport_scan=False, system=VFS),
    "deu": Requirements(0.0, gender=False, passport_scan=False, system="посольство Германии", phone=False),
    "grc": Requirements(0.0, gender=False, passport_scan=False, system="посольство Греции", phone=False),
}
DEFAULT = Requirements(0.0, gender=True, passport_scan=False, system=VFS)


def requirements(mission: str) -> Requirements:
    return REQUIREMENTS.get(mission, DEFAULT)


def form_steps(mission: str) -> list[str]:
    r = requirements(mission)
    steps = ["window", "full_name"]
    if r.gender:
        steps.append("gender")
    steps += ["birth", "nationality", "passport", "passport_exp"]
    if r.passport_scan:
        steps.append("passport_scan")
    steps.append("residence_exp")
    if r.phone:
        steps.append("phone")
    steps.append("email")
    return steps
