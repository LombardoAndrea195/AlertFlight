from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median

from .sources import Offer


@dataclass
class Deal:
    offer: Offer
    reference: float  # typical price for this destination

    @property
    def discount(self) -> float:
        return 1 - self.offer.price / self.reference


def best_per_window_city(offers: list[Offer]) -> dict[tuple[str, str], Offer]:
    best: dict[tuple[str, str], Offer] = {}
    for o in offers:
        key = (o.window.id, o.city)
        if key not in best or o.price < best[key].price:
            best[key] = o
    return best


def reference_prices(best: dict[tuple[str, str], Offer], state: dict, today: date, cfg: dict) -> dict[str, float]:
    """Typical price per destination, updating the history in state.

    The comparison is the median of each destination's daily median across all
    searched periods.  This makes a cheap weekend stand out immediately rather
    than waiting for several observations of the exact same future dates.
    """
    by_city: dict[str, list[float]] = {}
    for o in best.values():
        by_city.setdefault(o.city, []).append(o.price)

    baseline: dict[str, dict[str, float]] = state.setdefault("baseline", {})
    for city, prices in by_city.items():
        baseline.setdefault(city, {})[today.isoformat()] = round(median(prices), 2)
    cutoff = (today - timedelta(days=cfg["lookback_days"])).isoformat()
    for city in list(baseline):
        baseline[city] = {d: p for d, p in baseline[city].items() if d >= cutoff}
        if not baseline[city]:
            del baseline[city]

    return {
        city: round(median(baseline[city].values()), 2)
        for city, prices in by_city.items()
        if len(prices) >= cfg["min_samples"]
    }


def is_deal(price: float, ref: float | None, cfg: dict) -> bool:
    return ref is not None and price <= ref * (1 - cfg["discount"]) and ref - price >= cfg["min_saving_eur"]


def new_alerts(
    best: dict[tuple[str, str], Offer], refs: dict[str, float], state: dict, today: date, cfg: dict
) -> list[Deal]:
    """Deals not alerted yet, or that dropped a further `realert_drop` since the last alert."""
    alerted: dict[str, float] = state.setdefault("alerted", {})
    deals = []
    for o in best.values():
        ref = refs.get(o.city)
        if not is_deal(o.price, ref, cfg):
            continue
        key = f"{o.window.id}|{o.city}"
        if key in alerted and o.price > alerted[key] * (1 - cfg["realert_drop"]):
            continue
        deals.append(Deal(o, ref))

    deals.sort(key=lambda d: d.discount, reverse=True)
    deals = deals[: cfg["max_per_message"]]
    for d in deals:
        alerted[f"{d.offer.window.id}|{d.offer.city}"] = d.offer.price

    today_s = today.isoformat()
    state["alerted"] = {k: v for k, v in alerted.items() if k.split("|")[0].split("_")[1] >= today_s}
    return deals
