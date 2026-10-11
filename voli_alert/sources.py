import logging
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta

import requests

from .periods import Window

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", "Accept-Encoding": "gzip"}
TP_DATA = "https://api.travelpayouts.com/data"


@dataclass
class Offer:
    window: Window
    city: str  # IATA city code, used to merge sources
    price: float
    depart: str  # ISO datetime, local time
    ret: str
    origin_airport: str
    dest_airport: str
    stops: int
    carrier: str
    link: str
    trip: str = "ar"  # "ar" round trip; "andata" / "ritorno" one-way leg, with ret = ""


class Geo:
    """Airport → city and city → Italian name, from Travelpayouts public data."""

    def __init__(self) -> None:
        self.airport_city: dict[str, str] = {}
        self.city_name: dict[str, str] = {}
        self.city_country: dict[str, str] = {}
        self.country_name: dict[str, str] = {}
        try:
            for a in _get(f"{TP_DATA}/en/airports.json"):
                self.airport_city[a["code"]] = a["city_code"]
            for c in _get(f"{TP_DATA}/it/cities.json"):
                self.city_name[c["code"]] = c.get("name") or c["name_translations"].get("en") or c["code"]
                self.city_country[c["code"]] = c["country_code"]
            for c in _get(f"{TP_DATA}/it/countries.json"):
                self.country_name[c["code"]] = c.get("name") or c["name_translations"].get("en") or c["code"]
        except requests.RequestException as e:
            log.warning("Geo data unavailable, falling back to IATA codes: %s", e)

    def city_of(self, airport: str) -> str:
        return self.airport_city.get(airport, airport)

    def label(self, city: str) -> str:
        country = self.country_name.get(self.city_country.get(city, ""), "")
        name = self.city_name.get(city, city)
        return f"{name} ({country})" if country else name


def _get(url: str, params: dict | None = None, headers: dict | None = None, tries: int = 3, body: dict | None = None):
    for attempt in range(tries):
        try:
            r = requests.request("POST" if body else "GET", url, params=params, json=body,
                                 headers=HEADERS | (headers or {}), timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def ryanair(
    airport: str, w: Window, out: date, out_times: tuple[str, str], inb: list[date], in_from: str, geo: Geo
) -> list[Offer]:
    data = _get(
        "https://services-api.ryanair.com/farfnd/v4/roundTripFares",
        {
            "departureAirportIataCode": airport,
            "outboundDepartureDateFrom": out.isoformat(),
            "outboundDepartureDateTo": out.isoformat(),
            "outboundDepartureTimeFrom": out_times[0],
            "outboundDepartureTimeTo": out_times[1],
            "inboundDepartureDateFrom": min(inb).isoformat(),
            "inboundDepartureDateTo": max(inb).isoformat(),
            "inboundDepartureTimeFrom": in_from,
            "inboundDepartureTimeTo": "23:59",
            "currency": "EUR",
            "market": "it-it",
        },
    )
    offers = []
    for f in data.get("fares") or []:
        o, i = f["outbound"], f["inbound"]
        dest = o["arrivalAirport"]["iataCode"]
        city = geo.city_of(dest)
        if not geo.city_name.get(city, "").strip() or geo.city_name[city].startswith("Aeroporto"):
            geo.city_name[city] = o["arrivalAirport"]["city"]["name"]
        dep, ret = o["departureDate"], i["departureDate"]
        offers += [
            Offer(w, city, f["summary"]["price"]["value"], dep, ret, airport, dest, 0, "Ryanair",
                  _ryanair_link(airport, dest, dep, ret)),
            Offer(w, city, o["price"]["value"], dep, "", airport, dest, 0, "Ryanair",
                  _ryanair_link(airport, dest, dep), "andata"),
            Offer(w, city, i["price"]["value"], ret, "", dest, airport, 0, "Ryanair",
                  _ryanair_link(dest, airport, ret), "ritorno"),
        ]
    return offers


def _ryanair_link(origin: str, dest: str, out: str, back: str = "") -> str:
    return (
        f"https://www.ryanair.com/it/it/trip/flights/select?adults=1&isReturn={'true' if back else 'false'}"
        f"&dateOut={out[:10]}{f'&dateIn={back[:10]}' if back else ''}&originIata={origin}&destinationIata={dest}"
    )


WIZZ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/140.0.0.0 Safari/537.36",
    "Origin": "https://www.wizzair.com",
    "Referer": "https://www.wizzair.com/it-it/flights/timetable",
}


class WizzAir:
    """Wizz Air timetable API: cheapest fare per day on each route, both directions."""

    def __init__(self, airports: list[str], delay: float, cooldown: float) -> None:
        self.delay, self.cooldown = delay, cooldown
        page = requests.get("https://www.wizzair.com/it-it", headers=WIZZ_HEADERS, timeout=30)
        page.raise_for_status()
        m = re.search(r"be\.wizzair\.com/(\d+(?:\.\d+)+)", page.text)  # API version changes with each release
        if not m:
            raise RuntimeError("Wizz Air API version not found")
        self.api = f"https://be.wizzair.com/{m[1]}/Api"
        cities = _get(f"{self.api}/asset/map", {"languageCode": "it-it"}, WIZZ_HEADERS)["cities"]
        fake = {c["iata"] for c in cities if c.get("isFakeStation")}  # "London (All Airports)" and similar
        self.names = {c["iata"]: c["shortName"] for c in cities}
        self.routes = [
            (c["iata"], k["iata"]) for c in cities if c["iata"] in airports
            for k in c["connections"] if k["iata"] not in fake
        ]

    def _timetable(self, body: dict) -> dict:
        """Wizz Air answers 503 after ~50 quick requests: go slowly and wait it out when blocked."""
        for attempt in range(3):
            time.sleep(self.delay)
            try:
                return _get(f"{self.api}/search/timetable", headers=WIZZ_HEADERS, body=body, tries=1)
            except requests.HTTPError as e:
                if e.response is None or e.response.status_code not in (429, 503) or attempt == 2:
                    raise
                log.info("Wizz Air is throttling, waiting %ds", self.cooldown)
                time.sleep(self.cooldown)

    def offers(
        self, origin: str, dest: str, trips: list[tuple[Window, date, tuple[str, str]]], in_from: str, geo: Geo
    ) -> list[Offer]:
        out: dict[str, dict] = {}
        inb: dict[str, dict] = {}
        start, last = min(d for _, d, _ in trips), max(w.end for w, _, _ in trips)
        while start <= last:
            end = min(start + timedelta(days=41), last)  # the API accepts at most 42 days
            data = self._timetable({
                "flightList": [
                    {"departureStation": origin, "arrivalStation": dest, "from": str(start), "to": str(end)},
                    {"departureStation": dest, "arrivalStation": origin, "from": str(start), "to": str(end)},
                ],
                "priceType": "regular",
                "adultCount": 1,
                "childCount": 0,
                "infantCount": 0,
            })
            out |= {f["departureDate"][:10]: f for f in data.get("outboundFlights") or [] if _wizz_priced(f)}
            inb |= {f["departureDate"][:10]: f for f in data.get("returnFlights") or [] if _wizz_priced(f)}
            start = end + timedelta(days=1)

        city = geo.city_of(dest)
        if not geo.city_name.get(city, "").strip() or geo.city_name[city].startswith("Aeroporto"):
            geo.city_name[city] = self.names.get(dest, dest)
        offers = []
        for w, d, (lo, hi) in trips:
            o, i = out.get(str(d)), inb.get(str(w.end))
            # The fare is the cheapest of the day: keep the day if one of its flights fits the times
            dep = o and next((t for t in o["departureDates"] if lo <= t[11:16] <= hi), None)
            ret = i and next((t for t in i["departureDates"] if t[11:16] >= in_from), None)
            if dep:
                offers.append(Offer(w, city, o["price"]["amount"], dep, "", origin, dest, 0, "Wizz Air",
                                    _wizz_link(origin, dest, d), "andata"))
            if ret:
                offers.append(Offer(w, city, i["price"]["amount"], ret, "", dest, origin, 0, "Wizz Air",
                                    _wizz_link(dest, origin, w.end), "ritorno"))
            if dep and ret:
                offers.append(Offer(w, city, round(o["price"]["amount"] + i["price"]["amount"], 2), dep, ret,
                                    origin, dest, 0, "Wizz Air", _wizz_link(origin, dest, d, w.end)))
        return offers


def _wizz_link(origin: str, dest: str, out: date, back: date | None = None) -> str:
    return f"https://www.wizzair.com/it-it/booking/select-flight/{origin}/{dest}/{out}/{back or 'null'}/1/0/0/null"


def _wizz_priced(f: dict) -> bool:
    return f.get("priceType") == "price" and f["price"]["currencyCode"] == "EUR" and f["price"]["amount"] > 0


def travelpayouts(token: str, home: str, w: Window, out: date, trip: str = "ar") -> list[Offer]:
    """Cached Aviasales fares, all airlines worldwide. "ritorno" searches flights to home on the last day."""
    params = {"departure_at": out.isoformat(), "one_way": "true", "sorting": "price",
              "currency": "eur", "market": "it", "limit": 1000}
    if trip == "ritorno":
        params["destination"] = home
    else:
        params |= {"origin": home, "unique": "true"}
    if trip == "ar":
        params |= {"return_at": w.end.isoformat(), "one_way": "false"}
    data = _get("https://api.travelpayouts.com/aviasales/v3/prices_for_dates", params,
                headers={"X-Access-Token": token})
    offers = []
    for t in data.get("data") or []:
        stops = max(t.get("transfers") or 0, t.get("return_transfers") or 0)
        offers.append(
            Offer(w, t["origin"] if trip == "ritorno" else t["destination"], float(t["price"]),
                  t["departure_at"], (t.get("return_at") or "") if trip == "ar" else "",
                  t.get("origin_airport", t["origin"]), t.get("destination_airport", t["destination"]), stops,
                  t.get("airline", "?"), f"https://www.aviasales.com{t['link']}" if t.get("link") else "", trip)
        )
    return offers
