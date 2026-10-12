import argparse
import html
import json
import logging
import os
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

import requests

from . import sources
from .deals import Deal, best_per_window_city, new_alerts, reference_prices
from .periods import fmt_day, windows

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("voli_alert")


def out_times(d: date, w, t: dict) -> tuple[str, str]:
    return (t["day_before_from"], "23:59") if d < w.start else ("00:00", t["first_day_until"])


def times_ok(o: sources.Offer, t: dict) -> bool:
    if not has_time(o.depart):  # the source only knows the dates, already chosen to fit the window
        return True
    if o.trip == "ritorno":
        return o.depart[11:16] >= t["return_from"]
    lo, hi = out_times(date.fromisoformat(o.depart[:10]), o.window, t)
    return lo <= o.depart[11:16] <= hi and (o.trip == "andata" or o.ret[11:16] >= t["return_from"])


def has_time(iso: str) -> bool:
    return len(iso) >= 16


def run(job) -> tuple[list[sources.Offer], bool]:
    try:
        return job(), False
    except Exception as e:  # one failing call must not kill the daily run
        log.warning("%s", e)
        return [], True


def run_lane(jobs: list, delay: float = 0) -> tuple[list[sources.Offer], int, int]:
    """Run jobs one after the other: returns offers, calls and errors."""
    offers, errors = [], 0
    for job in jobs:
        found, failed = run(job)
        offers, errors = offers + found, errors + failed
        time.sleep(delay)
    return offers, len(jobs), errors


def setup(name: str, make):
    try:
        return make()
    except Exception as e:
        log.warning("%s unavailable: %s", name, e)
        return None


def periods_within(cfg: dict, today: date, wins: list, horizon_days: int) -> list[tuple]:
    """Windows starting within the horizon, with their outbound dates still ahead."""
    return [
        (w, out) for w in wins if (w.start - today).days <= horizon_days
        if (out := [d for d in w.outbound_dates(cfg["depart_day_before"]) if d > today])
    ]


def trips_within(cfg: dict, today: date, wins: list, horizon_days: int) -> list[tuple]:
    return [(w, d) for w, out in periods_within(cfg, today, wins, horizon_days) for d in out]


def wizz_lanes(cfg: dict, today: date, wins: list, geo: sources.Geo) -> list[list]:
    wz = cfg["wizzair"]
    wizz = setup("Wizz Air", lambda: sources.WizzAir(wz["airports"], wz["delay_seconds"], wz["cooldown_seconds"]))
    if not wizz:
        return []
    log.info("Wizz Air: %d routes", len(wizz.routes))
    t = cfg["times"]
    trips = [(w, d, out_times(d, w, t)) for w, d in trips_within(cfg, today, wins, wz["horizon_days"])]
    return [[lambda o=o, d=d: wizz.offers(o, d, trips, t["return_from"], geo) for o, d in wizz.routes]]


def vueling_lanes(cfg: dict, today: date, wins: list, geo: sources.Geo) -> list[list]:
    vy = cfg["vueling"]
    vueling = setup("Vueling", lambda: sources.Vueling(vy["airports"], vy["tries"]))
    if not vueling:
        return []
    log.info("Vueling: %d routes", len(vueling.routes))
    trips = trips_within(cfg, today, wins, vy["horizon_days"])
    paths = vueling.PATHS
    return [
        [lambda p=p, o=o, d=d: vueling.offers(p, o, d, trips, geo) for o, d in vueling.routes[i::len(paths)]]
        for i, p in enumerate(paths)
    ]


def lufthansa_lanes(cfg: dict, today: date, wins: list, geo: sources.Geo) -> list[list]:
    lh = cfg["lufthansa"]
    dests = setup("Lufthansa", lambda: sources.lufthansa_destinations(cfg["origin_city"]))
    if not dests:
        return []
    log.info("Lufthansa: %d destinations", len(dests))
    jobs = [
        lambda w=w, dest=dest, out=out: sources.lufthansa(lh["airport"], dest, w, out, geo)
        for w, out in periods_within(cfg, today, wins, lh["horizon_days"]) for dest in dests
    ]
    return [jobs[i::lh["workers"]] for i in range(lh["workers"])]


def iberia_lanes(cfg: dict, today: date, wins: list, geo: sources.Geo) -> list[list]:
    ib = cfg["iberia"]
    iberia = setup("Iberia", sources.Iberia)
    if not iberia:
        return []
    return [[
        lambda w=w, dest=dest, out=out: iberia.offers(ib["airport"], dest, w, out, geo)
        for w, out in periods_within(cfg, today, wins, ib["horizon_days"]) for dest in ib["destinations"]
    ]]


def collect(cfg: dict, today: date, geo: sources.Geo) -> tuple[list, list[sources.Offer]]:
    tp_token = os.environ.get("TRAVELPAYOUTS_TOKEN")
    if not tp_token:
        log.warning("TRAVELPAYOUTS_TOKEN not set: no other airlines and no intercontinental flights")

    t = cfg["times"]
    home = cfg["origin_city"]
    wins = windows(today, cfg["horizon_days"], cfg["max_bridge_days"], cfg.get("holiday_subdiv"))
    log.info("%d windows to check", len(wins))
    offers, errors, calls = [], 0, 0
    # Slow sources run in their own threads ("lanes"), alongside Ryanair and Travelpayouts
    lanes = [(lane, 0) for lane in wizz_lanes(cfg, today, wins, geo) + vueling_lanes(cfg, today, wins, geo)]
    lanes += [(lane, cfg["lufthansa"]["delay_seconds"]) for lane in lufthansa_lanes(cfg, today, wins, geo)]
    lanes += [(lane, cfg["iberia"]["delay_seconds"]) for lane in iberia_lanes(cfg, today, wins, geo)]
    with ThreadPoolExecutor(max(len(lanes), 1)) as pool:
        slow = [pool.submit(run_lane, lane, delay) for lane, delay in lanes]
        jobs = []
        for w in wins:
            out = [d for d in w.outbound_dates(cfg["depart_day_before"]) if d > today]
            jobs += [
                lambda w=w, a=a, d=d: sources.ryanair(a, w, d, out_times(d, w, t), [w.end], t["return_from"], geo)
                for a in cfg["ryanair_airports"] for d in out
            ]
            if tp_token:
                jobs += [lambda w=w, d=d, k=k: sources.travelpayouts(tp_token, home, w, d, k)
                         for d in out for k in ("ar", "andata")]
                jobs.append(lambda w=w: sources.travelpayouts(tp_token, home, w, w.end, "ritorno"))
        for found, n, failed in [run_lane(jobs, cfg["delay_seconds"])] + [f.result() for f in slow]:
            calls, errors, offers = calls + n, errors + failed, offers + found
    log.info("%d calls, %d errors, %d offers", calls, errors, len(offers))
    if calls and errors == calls:
        raise SystemExit("Every request failed")
    return wins, [o for o in offers if times_ok(o, t)]


def fmt_time(iso: str) -> str:
    dt = datetime.fromisoformat(iso[:16])
    return f"{fmt_day(dt.date())} {dt:%H:%M}" if has_time(iso) else fmt_day(dt.date())


def fmt_stops(stops: int) -> str:
    return {-1: "scali da verificare", 0: "diretto", 1: "1 scalo"}.get(stops, f"{stops} scali")


def format_message(deals: list[Deal], geo: sources.Geo, origin_label: str) -> str:
    lines = [f"✈️ <b>{len(deals)} voli sotto il prezzo normale da {origin_label}</b>"]
    by_window: dict[str, list[Deal]] = {}
    for d in sorted(deals, key=lambda d: (d.offer.window.start, d.offer.price)):
        by_window.setdefault(d.offer.window.describe(), []).append(d)
    for title, items in by_window.items():
        lines.append(f"\n🗓 <b>{html.escape(title)}</b>")
        for d in items:
            o = d.offer
            book = f' · <a href="{html.escape(o.link)}">prenota</a>' if o.link else ""
            unknown = "" if has_time(o.depart) else " · orario da verificare"
            lines.append(
                f"• <b>{html.escape(geo.label(o.city))}</b> — <b>{o.price:.0f} €</b> "
                f"invece di ~{d.reference:.0f} € (−{d.discount:.0%})\n"
                f"   {o.origin_airport}⇄{o.dest_airport} · andata {fmt_time(o.depart)} · ritorno {fmt_time(o.ret)}"
                f"{unknown} · {fmt_stops(o.stops)} · {html.escape(o.carrier)}{book}"
            )
    return "\n".join(lines)


def send_telegram(text: str) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        log.warning("Telegram not configured, printing instead")
        print(text)
        return
    chunk = ""
    for line in text.split("\n"):  # Telegram caps messages at 4096 chars
        if len(chunk) + len(line) + 1 > 4000:
            _post(token, chat, chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        _post(token, chat, chunk)


def _post(token: str, chat: str, text: str) -> None:
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=30,
    )
    r.raise_for_status()


def site_data(cfg, wins, trips: dict, deals, geo: sources.Geo) -> dict:
    """Everything the web page shows, as compact JSON. `trips` maps trip type to (best offers, typical prices)."""
    index = {w.id: i for i, w in enumerate(wins)}
    alerted = {(d.offer.window.id, d.offer.city) for d in deals}
    cities = {o.city for best, _ in trips.values() for o in best.values()}

    def fares(trip: str) -> list[dict]:
        best, refs = trips[trip]
        return [
            {
                "w": index[o.window.id],
                "c": o.city,
                "p": round(o.price, 2),
                "r": refs.get(o.city),
                "d": o.depart[:16],
                **({"b": o.ret[:16]} if o.ret else {}),
                "o": o.origin_airport,
                "a": o.dest_airport,
                "s": o.stops,
                "k": o.carrier,
                "l": o.link,
                **({"n": 1} if trip == "ar" and (o.window.id, o.city) in alerted else {}),
            }
            for o in sorted(best.values(), key=lambda o: (index[o.window.id], o.price))
        ]

    return {
        "generated": datetime.now().astimezone().isoformat(timespec="minutes"),
        "origin": cfg["origin_label"],
        "rules": {k: cfg["deals"][k] for k in ("discount", "min_saving_eur")},
        "windows": [
            {
                "start": w.start.isoformat(),
                "end": w.end.isoformat(),
                "label": w.label.removeprefix("Ponte "),
                "kind": "weekend" if w.label == "Weekend" else "ponte" if w.label.startswith("Ponte ") else "festivo",
            }
            for w in wins
        ],
        "places": {
            c: [geo.city_name.get(c, c), geo.country_name.get(geo.city_country.get(c, ""), "")] for c in sorted(cities)
        },
        "fares": fares("ar"),
        "andata": fares("andata"),
        "ritorno": fares("ritorno"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily cheap-flight alert for weekends and long weekends")
    parser.add_argument("--dry-run", action="store_true", help="print the message, do not send or save state")
    parser.add_argument("--config", default=ROOT / "config.toml", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cfg = tomllib.loads(args.config.read_text())
    state_path = ROOT / cfg["state_file"]
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    today = date.today()

    geo = sources.Geo()
    wins, offers = collect(cfg, today, geo)
    trips = {}
    for trip in ("ar", "andata", "ritorno"):
        best = best_per_window_city([o for o in offers if o.trip == trip])
        # One-way legs keep their own price history, apart from round trips
        trips[trip] = best, reference_prices(best, state if trip == "ar" else state.setdefault(trip, {}),
                                             today, cfg["deals"])
    best, refs = trips["ar"]
    deals = new_alerts(best, refs, state, today, cfg["deals"])
    log.info("%d new deals", len(deals))

    site_path = ROOT / cfg["site_data_file"]
    site_path.parent.mkdir(parents=True, exist_ok=True)
    site_path.write_text(json.dumps(site_data(cfg, wins, trips, deals, geo), ensure_ascii=False, separators=(",", ":")))

    message = format_message(deals, geo, cfg["origin_label"]) if deals else None
    if args.dry_run:
        print(message or "Nessun affare oggi.")
        return
    if message:
        send_telegram(message)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, separators=(",", ":"), sort_keys=True))


if __name__ == "__main__":
    main()
