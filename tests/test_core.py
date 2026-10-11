from datetime import date

from voli_alert.deals import best_per_window_city, new_alerts, reference_prices
from voli_alert.periods import Window, windows
from voli_alert.sources import Offer

CFG = {"discount": 0.5, "min_saving_eur": 15, "min_samples": 5, "lookback_days": 90,
       "realert_drop": 0.10, "max_per_message": 40}


def test_plain_weekend():
    ws = windows(date(2026, 10, 11), 14, 1, "RM")
    assert [(w.start, w.end, w.label) for w in ws] == [
        (date(2026, 10, 17), date(2026, 10, 18), "Weekend"),
        (date(2026, 10, 24), date(2026, 10, 25), "Weekend"),
    ]
    assert ws[0].outbound_dates(True) == [date(2026, 10, 16), date(2026, 10, 17)]


def test_tuesday_holiday_becomes_bridge():
    # 8 Dec 2026 is a Tuesday: take Monday off
    w = next(w for w in windows(date(2026, 11, 30), 30, 1, "RM") if w.end == date(2026, 12, 8))
    assert w.start == date(2026, 12, 5)
    assert w.label == "Ponte Immacolata Concezione"


def test_wednesday_holiday_needs_two_days():
    # 2 Jun 2027 is a Wednesday
    ends = [w.end for w in windows(date(2027, 5, 25), 20, 1, "RM")]
    assert date(2027, 6, 2) not in ends
    ends = [w.end for w in windows(date(2027, 5, 25), 20, 2, "RM")]
    assert date(2027, 6, 6) in ends


def find_deals(best, state, today, cfg):
    return new_alerts(best, reference_prices(best, state, today, cfg), state, today, cfg)


def offer(w: Window, city: str, price: float) -> Offer:
    return Offer(w, city, price, f"{w.start}T10:00", f"{w.end}T18:00", "FCO", city, 0, "FR", "")


def test_flags_only_anomalous_prices_once():
    ws = windows(date(2026, 10, 11), 70, 1, "RM")
    offers = [offer(w, "BCN", 100) for w in ws] + [offer(ws[3], "BCN", 40), offer(ws[4], "BCN", 60)]
    state: dict = {}
    deals = find_deals(best_per_window_city(offers), state, date(2026, 10, 11), CFG)
    assert [(d.offer.window, d.offer.price) for d in deals] == [(ws[3], 40)]

    # same price tomorrow: no repeat; a further 10% drop: alert again
    assert find_deals(best_per_window_city(offers), state, date(2026, 10, 12), CFG) == []
    offers[-2] = offer(ws[3], "BCN", 35)
    assert len(find_deals(best_per_window_city(offers), state, date(2026, 10, 13), CFG)) == 1


def test_needs_enough_samples():
    ws = windows(date(2026, 10, 11), 30, 1, "RM")
    offers = [offer(ws[0], "JFK", 100), offer(ws[1], "JFK", 10)]
    assert find_deals(best_per_window_city(offers), {}, date(2026, 10, 11), CFG) == []
