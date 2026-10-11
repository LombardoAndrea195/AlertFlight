import logging
import time
from dataclasses import dataclass
from datetime import date

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


def _get(url: str, params: dict | None = None, headers: dict | None = None, tries: int = 3):
    for attempt in range(tries):
        try:
            r = requests.get(url, params=params, headers=HEADERS | (headers or {}), timeout=30)
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
        link = (
            "https://www.ryanair.com/it/it/trip/flights/select?adults=1&isReturn=true"
            f"&dateOut={o['departureDate'][:10]}&dateIn={i['departureDate'][:10]}"
            f"&originIata={airport}&destinationIata={dest}"
        )
        offers.append(
            Offer(w, city, f["summary"]["price"]["value"], o["departureDate"], i["departureDate"],
                  airport, dest, 0, "Ryanair", link)
        )
    return offers


def travelpayouts(token: str, origin: str, w: Window, out: date, inb: date) -> list[Offer]:
    data = _get(
        "https://api.travelpayouts.com/aviasales/v3/prices_for_dates",
        {
            "origin": origin,
            "departure_at": out.isoformat(),
            "return_at": inb.isoformat(),
            "one_way": "false",
            "unique": "true",
            "sorting": "price",
            "currency": "eur",
            "market": "it",
            "limit": 1000,
        },
        headers={"X-Access-Token": token},
    )
    offers = []
    for t in data.get("data") or []:
        stops = max(t.get("transfers") or 0, t.get("return_transfers") or 0)
        offers.append(
            Offer(w, t["destination"], float(t["price"]), t["departure_at"], t["return_at"],
                  t.get("origin_airport", origin), t.get("destination_airport", t["destination"]), stops,
                  t.get("airline", "?"), f"https://www.aviasales.com{t['link']}" if t.get("link") else "")
        )
    return offers
