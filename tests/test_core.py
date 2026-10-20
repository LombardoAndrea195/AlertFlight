from datetime import date

from voli_alert.__main__ import times_ok
from voli_alert.deals import best_per_window_city, new_alerts, reference_prices
from voli_alert.periods import Window, windows
from voli_alert.sources import Geo, Offer, _daily, _months

CFG = {"discount": 0.5, "min_saving_eur": 15, "min_samples": 5, "lookback_days": 90,
       "realert_drop": 0.10, "max_per_message": 40}


def test_plain_weekend():
    ws = windows(date(2026, 10, 11), 14, 0, "RM")
    assert [(w.start, w.end, w.label) for w in ws] == [
        (date(2026, 10, 17), date(2026, 10, 18), "Weekend"),
        (date(2026, 10, 24), date(2026, 10, 25), "Weekend"),
    ]
    assert ws[0].outbound_dates(True) == [date(2026, 10, 16), date(2026, 10, 17)]


def test_leave_days_can_extend_a_weekend_on_both_sides():
    ws = windows(date(2026, 10, 11), 14, 2, "RM")
    thursday_to_sunday = next(w for w in ws if w.start == date(2026, 10, 15) and w.end == date(2026, 10, 18))
    saturday_to_monday = next(w for w in ws if w.start == date(2026, 10, 17) and w.end == date(2026, 10, 19))
    assert thursday_to_sunday.leave_days == (date(2026, 10, 15), date(2026, 10, 16))
    assert thursday_to_sunday.outbound_dates(True) == [date(2026, 10, 15)]
    assert saturday_to_monday.leave_days == (date(2026, 10, 19),)
    assert saturday_to_monday.outbound_dates(True) == [date(2026, 10, 16), date(2026, 10, 17)]


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


def test_keeps_the_plain_weekend_alongside_the_better_bridge():
    ws = windows(date(2027, 5, 25), 20, 2, "RM")
    plain = next(w for w in ws if w.start == date(2027, 5, 29) and w.end == date(2027, 5, 30))
    bridge = next(w for w in ws if w.start == date(2027, 5, 29) and w.end == date(2027, 6, 2))
    assert plain.leave_days == ()
    assert bridge.leave_days == (date(2027, 5, 31), date(2027, 6, 1))


def find_deals(best, state, today, cfg):
    return new_alerts(best, reference_prices(best, state, today, cfg), state, today, cfg)


def offer(w: Window, city: str, price: float) -> Offer:
    return Offer(w, city, price, f"{w.start}T10:00", f"{w.end}T18:00", "FCO", city, 0, "FR", "")


def test_flags_only_anomalous_prices_once():
    ws = windows(date(2026, 10, 11), 70, 1, "RM")
    offers = [offer(w, "BCN", 100) for w in ws] + [offer(ws[3], "BCN", 40), offer(ws[4], "BCN", 60)]
    state = {}
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


def test_one_way_times():
    t = {"day_before_from": "18:00", "first_day_until": "14:00", "return_from": "12:00", "return_until": "23:00"}
    w = windows(date(2026, 10, 11), 7, 0, "RM")[0]  # sat 17 - sun 18 Oct

    def leg(when: str, trip: str) -> Offer:
        return Offer(w, "BCN", 20, when, "", "FCO", "BCN", 0, "FR", "", trip)

    assert times_ok(leg("2026-10-16T18:30", "andata"), t)
    assert not times_ok(leg("2026-10-16T09:00", "andata"), t)  # friday morning needs a day off
    assert times_ok(leg("2026-10-18T19:00", "ritorno"), t)
    assert not times_ok(leg("2026-10-18T08:00", "ritorno"), t)  # sunday morning wastes the day
    assert not times_ok(leg("2026-10-18T23:30", "ritorno"), t)


def test_dates_without_times_are_kept():
    t = {"day_before_from": "18:00", "first_day_until": "14:00", "return_from": "12:00", "return_until": "23:00"}
    w = windows(date(2026, 10, 11), 7, 0, "RM")[0]
    assert times_ok(Offer(w, "MAD", 200, "2026-10-16", "2026-10-18", "FCO", "MAD", 0, "Iberia", ""), t)


def test_daily_fares_become_legs_and_round_trips():
    w = windows(date(2026, 10, 11), 7, 0, "RM")[0]  # sat 17 - sun 18 Oct
    trips = [(w, d) for d in w.outbound_dates(True)]
    out = {"2026-10-16": (30.0, "2026-10-16", 0)}
    inb = {"2026-10-18": (25.5, "2026-10-18", 1)}
    geo = Geo.__new__(Geo)  # no network
    geo.airport_city, geo.city_name = {}, {}
    offers = _daily("X", "FCO", "BCN", out, inb, trips, geo, lambda *a: "")
    assert {(o.trip, o.price, o.depart, o.ret, o.stops) for o in offers} == {
        ("andata", 30.0, "2026-10-16", "", 0),
        ("ritorno", 25.5, "2026-10-18", "", 1),
        ("ar", 55.5, "2026-10-16", "2026-10-18", 1),
    }


def test_months():
    w = windows(date(2026, 10, 11), 120, 1, "RM")
    trips = [(x, x.start) for x in w]
    assert _months(trips) == [date(2026, 10, 1), date(2026, 11, 1), date(2026, 12, 1), date(2027, 1, 1),
                              date(2027, 2, 1)]
    assert _months(trips, 3) == [date(2026, 10, 1), date(2027, 1, 1)]
