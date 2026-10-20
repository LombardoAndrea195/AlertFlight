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
    leave_days: tuple[date, ...] = ()

    @property
    def id(self) -> str:
        return f"{self.start}_{self.end}"

    def outbound_dates(self, depart_day_before: bool) -> list[date]:
        """Use the evening before a normal break, otherwise its first free day.

        A plain Saturday–Sunday window therefore has the requested Friday-evening
        departure.  If taking leave makes Thursday the first free day, querying
        Thursday is more useful than leaving on Wednesday evening.
        """
        if depart_day_before and self.start not in self.leave_days:
            return [self.start - timedelta(days=1)]
        return [self.start]

    def describe(self) -> str:
        return f"{self.label} · {fmt_day(self.start)} – {fmt_day(self.end)}"


def windows(today: date, horizon_days: int, max_bridge_days: int, subdiv: str | None) -> list[Window]:
    """Every useful continuous break that needs at most ``max_bridge_days`` leave days.

    We enumerate short date ranges instead of only joining pre-existing days off.
    This preserves plain weekends and also finds the useful extensions on either
    side: for example, a Thursday holiday plus Friday leave, or a Monday leave
    after a weekend.  The leave-day cap makes the scan bounded and cheap.
    """
    first, last = today + timedelta(days=1), today + timedelta(days=horizon_days + 7)
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    hol = holidays.Italy(subdiv=subdiv, years=range(first.year, last.year + 1))

    naturally_off = {d: d.weekday() >= 5 or d in hol for d in days}
    result: dict[tuple[date, date], Window] = {}

    for start_i, start in enumerate(days):
        leave: list[date] = []
        naturally_off_days = 0
        for end in days[start_i:]:
            if naturally_off[end]:
                naturally_off_days += 1
            else:
                leave.append(end)
            if len(leave) > max_bridge_days:
                break
            if start > today + timedelta(days=horizon_days):
                continue
            # A single holiday plus a leave day is not a useful break on its
            # own.  At least two naturally free days keeps the search focused
            # on weekends, holidays joined to them, and genuine bridges.
            if end - start < timedelta(days=1) or naturally_off_days < 2:
                continue
            block = [d for d in days if start <= d <= end]
            names = list(dict.fromkeys(hol[d].replace("; ", " / ") for d in block if d in hol))
            label = " + ".join(names) if names else "Weekend"
            if leave:
                label = f"Ponte {label}"
            result[(start, end)] = Window(start, end, label, tuple(leave))

    return sorted(result.values(), key=lambda w: (w.start, w.end, len(w.leave_days)))
