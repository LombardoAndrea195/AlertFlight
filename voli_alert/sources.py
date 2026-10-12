import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta

import requests
from curl_cffi import requests as cffi

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
    stops: int  # -1 when the source does not say
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


class Vueling:
    """Vueling low-fare calendar: the cheapest flight of each day for a year, one direction per call.

    The CDN caches each path for 5 minutes ignoring the query string, so it may answer with another
    route: the answer is checked and, if wrong, asked again once the cached copy expires. Each path
    variant has its own cache, so routes are spread across them and fetched in parallel.
    """

    API = "https://apiw.vueling.com/api"
    PATHS = ["v1/availability", "v1/availability/", "v2/availability", "v2/availability/"]

    def __init__(self, airports: list[str], tries: int) -> None:
        self.tries = tries
        stations = _get("https://static.vueling.com/corporativeCMS/Stations/it-IT.json")["stations"]["value"]
        self.routes, self.names = [], {}
        for s in stations:
            if s["code"] not in airports:
                continue
            direct = {r["code"] for r in s["routes"] if not r["isConnection"]}  # includes city groups like PAR
            for r in s["routes"]:
                if (r["code"] in direct and not r["isMac"]) or r["macCode"] in direct:
                    self.routes.append((s["code"], r["code"]))
                    self.names[r["code"]] = r["name"]

    def _calendar(self, path: str, origin: str, dest: str) -> list[dict]:
        params = {"originCode": origin, "destinationCode": dest, "currencyCode": "EUR",
                  "providerName": "DmpsRetrieveAvailabilityProvider"}
        if path.startswith("v2"):
            params["includeSummary"] = "true"
        for _ in range(self.tries):
            r = requests.get(f"{self.API}/{path}", params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            value = r.json()["availability"]["value"]
            flights = value["data"] if isinstance(value, dict) else value
            if not flights or (flights[0]["departureStation"], flights[0]["arrivalStation"]) == (origin, dest):
                return flights
            m = re.search(r"max-age=(\d+)", r.headers.get("Cache-Control", ""))
            time.sleep(int(m[1]) + 1 if m else 60)
        raise RuntimeError(f"Vueling {origin}-{dest}: the cache keeps answering with another route")

    def offers(self, path: str, origin: str, dest: str, trips: list[tuple[Window, date]], geo: Geo) -> list[Offer]:
        def by_day(o: str, d: str) -> dict[str, dict]:
            return {f["departureDate"][:10]: f for f in self._calendar(path, o, d)
                    if f["currency"] == "EUR" and f["price"] > 0 and not f["isInvalidPrice"]}

        out, inb = by_day(origin, dest), by_day(dest, origin)
        city = geo.city_of(dest)
        if not geo.city_name.get(city, "").strip() or geo.city_name[city].startswith("Aeroporto"):
            geo.city_name[city] = self.names.get(dest, dest)
        offers = []
        for w, d in trips:
            o, i = out.get(str(d)), inb.get(str(w.end))
            if o:
                offers.append(Offer(w, city, o["price"], o["departureDate"][:16], "", origin, dest,
                                    _vueling_stops(o), "Vueling", _vueling_link(origin, dest, d), "andata"))
            if i:
                offers.append(Offer(w, city, i["price"], i["departureDate"][:16], "", dest, origin,
                                    _vueling_stops(i), "Vueling", _vueling_link(dest, origin, w.end), "ritorno"))
            if o and i:
                offers.append(Offer(w, city, o["price"] + i["price"], o["departureDate"][:16],
                                    i["departureDate"][:16], origin, dest, max(_vueling_stops(o), _vueling_stops(i)),
                                    "Vueling", _vueling_link(origin, dest, d, w.end)))
        return offers


def _vueling_stops(f: dict) -> int:
    return 1 if f.get("isConnectionFlight") else 0


def _vueling_link(origin: str, dest: str, out: date, back: date | None = None) -> str:
    return (f"https://tickets.vueling.com/booking/flightSearch?o={origin}&d={dest}&dd={out}"
            f"{f'&rd={back}' if back else ''}&adt=1&chd=0&inf=0&c=it&cur=EUR")


LH_HEADERS = {"X-Portal": "LH", "X-Portal-Language": "en", "X-Portal-Site": "IT", "Accept": "application/json"}
# From GitHub Actions IPs Lufthansa (Cloudflare) refuses plain requests and Iberia (Akamai) refuses Chrome's
# fingerprint: both accept curl_cffi impersonating Firefox
BROWSER = "firefox"
_local = threading.local()


def _browser() -> cffi.Session:
    """One curl_cffi session per thread: sessions are not thread-safe."""
    if not hasattr(_local, "session"):
        _local.session = cffi.Session(impersonate=BROWSER, timeout=60)
    return _local.session


def lufthansa_destinations(home: str) -> list[str]:
    """Cities Lufthansa Group advertises from home (a curated list, including long haul)."""
    r = _browser().post(f"https://www.lufthansa.com/service/api/lhg-fare-teaser/fareteaser/offers/{home}",
                        headers=LH_HEADERS, json={"touchPointId": "200", "decisionTypeId": "50", "tripType": "R",
                                                  "timestamp": f"{date.today()}T00:00:00.000Z", "timezone": 0})
    r.raise_for_status()
    return [o["city"] for o in r.json()["fareTeaserOffers"]]


def lufthansa(origin: str, dest: str, w: Window, out: list[date], geo: Geo) -> list[Offer]:
    """Cheapest Lufthansa Group round trip for each date pair. The API gives no flight times."""
    offers = []
    for d in out:
        r = _browser().post("https://www.lufthansa.com/service/api/teaser/offerbyday", headers=LH_HEADERS,
                            json={"cabin": "E", "tripType": "R", "origin": origin, "destination": dest,
                                  "outboundDate": str(d), "returnDate": str(w.end)})
        r.raise_for_status()
        if r.status_code == 204:  # no fare on these dates
            continue
        f = r.json()
        if f["currency"] == "EUR" and f["price"] > 0:
            link = (f"https://www.lufthansa.com/it/it/homepage?OriginCode={origin}&DestinationCode={dest}&TripType=R"
                    f"&DepartureDate={d}&ReturnDate={w.end}&PaxAdults=1&Cabin=E")
            offers.append(Offer(w, geo.city_of(dest), f["price"], str(d), str(w.end), origin, dest, -1, "Lufthansa",
                                link))
    return offers


class Iberia:
    """Iberia fare calendar: round-trip prices for 7×7 date pairs, no flight times. Behind Akamai."""

    AUTH = "https://ibisauth.iberia.com/api/auth/realms/commercial_platform/protocol/openid-connect/token"
    WEB_APP = "Basic aWJlcmlhX3dlYjo5ZGM4NzZjYi0xMDVkLTQ4MWItODM4Yy01NGUyNGQ3NDEwYzk="  # public, from iberia.com

    def __init__(self) -> None:
        self.session = cffi.Session(impersonate=BROWSER, timeout=60)
        self.token_until = 0.0

    def _auth(self) -> None:
        if time.monotonic() < self.token_until:
            return
        r = self.session.post(self.AUTH, headers={"Authorization": self.WEB_APP}, data={"grant_type": "client_credentials"})
        r.raise_for_status()
        t = r.json()
        self.session.headers["Authorization"] = f"Bearer {t['access_token']}"
        self.token_until = time.monotonic() + t["expires_in"] - 60

    def offers(self, origin: str, dest: str, w: Window, out: list[date], geo: Geo) -> list[Offer]:
        self._auth()
        slices = [{"origin": a, "destination": b, "date": str(d), "daysBefore": 3, "daysAfter": 3}
                  for a, b, d in ((origin, dest, w.start), (dest, origin, w.end))]
        r = self.session.post("https://ibisservices.iberia.com/api/sse-avm/rs/v2/calendar",
                              json={"marketCode": "IT", "passengers": [{"passengerType": "ADULT", "count": 1}],
                                    "slices": slices})
        r.raise_for_status()
        grid = r.json()["calendarGrid"]
        back = [c["inboundDate"] for c in grid["columns"]]
        offers = []
        for row in grid["rows"]:
            d = date.fromisoformat(row["outboundDate"])
            if d not in out:
                continue
            cell = row["cells"][back.index(str(w.end))]
            price = cell.get("adultPrice") or {}
            if cell.get("available") and price.get("currency") == "EUR" and price.get("total"):
                offers.append(Offer(w, geo.city_of(dest), price["total"], str(d), str(w.end), origin, dest, 0,
                                    "Iberia", _iberia_link(origin, dest, d, w.end)))
        return offers


def _iberia_link(origin: str, dest: str, out: date, back: date) -> str:
    return (f"https://www.iberia.com/flights/?market=IT&language=it&TRIP_TYPE=2&BEGIN_CITY_01={origin}"
            f"&END_CITY_01={dest}&BEGIN_DAY_01={out.day}&BEGIN_MONTH_01={out:%Y%m}&END_DAY_01={back.day}"
            f"&END_MONTH_01={back:%Y%m}&ADT=1&CHD=0&INF=0&BNN=0&YTH=0&YCD=0&FARE_TYPE=R")


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
