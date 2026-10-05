"""What each submission system needs to book a slot, and what it charges at booking time.

VFS field flags come from VFS's own form configuration (GET /configuration/fields/{mission}/cyp, 05.10.2026):
no document upload anywhere; Finland hides gender, date of birth and passport expiry but asks a reference number;
Austria has payOnlineEnabled=false (fee is paid at the centre); names allow only latin letters and spaces.

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
    birth: bool = True
    passport_exp: bool = True
    online_payment: bool = False   # VFS payOnlineEnabled


VFS = "VFS Global"
REQUIREMENTS: dict[str, Requirements] = {
    "hun": Requirements(22.80, gender=True, passport_scan=False, system=VFS, online_payment=True),
    "aut": Requirements(0.0, gender=True, passport_scan=False, system=VFS, online_payment=False),
    "svk": Requirements(40.00, gender=True, passport_scan=False, system=VFS, online_payment=True),
    "fin": Requirements(0.0, gender=False, passport_scan=False, system=VFS, birth=False, passport_exp=False,
                        online_payment=True),
    "hrv": Requirements(0.0, gender=True, passport_scan=False, system=VFS, online_payment=True),
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
    if r.birth:
        steps.append("birth")
    steps += ["nationality", "passport"]
    if r.passport_exp:
        steps.append("passport_exp")
    if r.passport_scan:
        steps.append("passport_scan")
    steps.append("residence_exp")
    if r.phone:
        steps.append("phone")
    # no client email: bookings use the service mailbox, confirmations are delivered through the bot
    return steps
