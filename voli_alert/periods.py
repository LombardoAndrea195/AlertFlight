from dataclasses import dataclass
from datetime import date, timedelta

import holidays

MESI = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]
GIORNI = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]


def fmt_day(d: date) -> str:
    return f"{GIORNI[d.weekday()]} {d.day} {MESI[d.month - 1]}"


@dataclass(frozen=True)
class Window:
    start: date  # first day off
    end: date  # last day off
    label: str

    @property
    def id(self) -> str:
        return f"{self.start}_{self.end}"

    def outbound_dates(self, depart_day_before: bool) -> list[date]:
        return ([self.start - timedelta(days=1)] if depart_day_before else []) + [self.start]

    def describe(self) -> str:
        return f"{self.label} · {fmt_day(self.start)} – {fmt_day(self.end)}"


def windows(today: date, horizon_days: int, max_bridge_days: int, subdiv: str | None) -> list[Window]:
    """Weekends and long weekends (holidays + bridge days) starting within the horizon."""
    first, last = today + timedelta(days=1), today + timedelta(days=horizon_days + 7)
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    hol = holidays.Italy(subdiv=subdiv, years=range(first.year, last.year + 1))

    off = {d: d.weekday() >= 5 or d in hol for d in days}
    bridged: set[date] = set()

    i = 0
    while i < len(days):
        if off[days[i]]:
            i += 1
            continue
        j = i
        while j < len(days) and not off[days[j]]:
            j += 1
        if 0 < i and j < len(days) and j - i <= max_bridge_days:
            bridged.update(days[i:j])
        i = j
    for d in bridged:
        off[d] = True

    result = []
    i = 0
    while i < len(days):
        if not off[days[i]]:
            i += 1
            continue
        j = i
        while j < len(days) and off[days[j]]:
            j += 1
        block = days[i:j]
        if len(block) >= 2 and block[0] <= today + timedelta(days=horizon_days):
            names = list(dict.fromkeys(hol[d].replace("; ", " / ") for d in block if d in hol))
            label = " + ".join(names) if names else "Weekend"
            if any(d in bridged for d in block):
                label = f"Ponte {label}"
            result.append(Window(block[0], block[-1], label))
        i = j
    return result
