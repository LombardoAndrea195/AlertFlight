import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta
from functools import partial
from urllib.parse import quote

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


def _name(geo: Geo, city: str, name: str) -> None:
    """Use the airline's name for cities the geo data does not know."""
    if name and (not geo.city_name.get(city, "").strip() or geo.city_name[city].startswith("Aeroporto")):
        geo.city_name[city] = name


Day = tuple[float, str, int]  # cheapest fare of a day: price, departure (ISO date, or date and time), stops


def _by_day(fares) -> dict[str, Day]:
    """Cheapest fare per day from (day, price, departure, stops) tuples."""
    days: dict[str, Day] = {}
    for day, price, when, stops in fares:
        if price and price > 0 and (day not in days or price < days[day][0]):
            days[day] = (float(price), when, stops)
    return days


def _daily(carrier: str, origin: str, dest: str, out: dict[str, Day], inb: dict[str, Day],
           trips: list[tuple[Window, date]], geo: Geo, link, name: str = "") -> list[Offer]:
    """Offers from the cheapest fare of each day in both directions: the two legs and their sum as a round trip."""
    city = geo.city_of(dest)
    _name(geo, city, name)
    offers = []
    for w, d in trips:
        o, i = out.get(str(d)), inb.get(str(w.end))
        if o:
            offers.append(Offer(w, city, o[0], o[1], "", origin, dest, o[2], carrier, link(origin, dest, d), "andata"))
        if i:
            offers.append(Offer(w, city, i[0], i[1], "", dest, origin, i[2], carrier, link(dest, origin, w.end),
                                "ritorno"))
        if o and i:
            stops = -1 if -1 in (o[2], i[2]) else max(o[2], i[2])
            offers.append(Offer(w, city, round(o[0] + i[0], 2), o[1], i[1], origin, dest, stops, carrier,
                                link(origin, dest, d, w.end)))
    return offers


def _add_months(d: date, n: int) -> date:
    """First day of the month n months after d's month."""
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def _months(trips: list[tuple[Window, date]], step: int = 1) -> list[date]:
    """First day of every step-th month, from the first outbound date to the last return date."""
    if not trips:
        return []
    first, last = _add_months(min(d for _, d in trips), 0), max(w.end for w, _ in trips)
    return [_add_months(first, i) for i in range(0, 1000, step) if _add_months(first, i) <= last]


def _aviasales_link(origin: str, dest: str, out: date | str, back: date | str | None = None) -> str:
    """Aviasales search (it compares every airline), for airlines without a usable booking link."""
    def ddmm(d: date | str) -> str:
        return f"{str(d)[8:10]}{str(d)[5:7]}"

    return f"https://www.aviasales.com/search/{origin}{ddmm(out)}{dest}{ddmm(back) if back else ''}1?currency=eur"


class _Site:
    """Browser session for APIs behind Akamai or Cloudflare: opens the home page first to get the anti-bot
    cookies and, when blocked, starts over with another browser fingerprint."""

    BROWSERS = ["firefox", "safari", "chrome"]

    def __init__(self, home: str) -> None:
        self.home, self.browser = home, -1
        self.page = self._open()

    def _open(self) -> str:
        self.browser += 1
        self.session = cffi.Session(impersonate=self.BROWSERS[self.browser % len(self.BROWSERS)], timeout=60)
        r = self.session.get(self.home)
        r.raise_for_status()
        return r.text

    def json(self, method: str, url: str, **kwargs):
        for _ in self.BROWSERS:
            r = self.session.request(method, url, **kwargs)
            if r.status_code not in (403, 429):
                r.raise_for_status()
                return r.json()
            log.info("%s is blocking, starting over", self.home)
            time.sleep(5)
            self._open()
        raise RuntimeError(f"{url}: blocked")


def _next_data(page: str) -> dict:
    """The JSON a Next.js page is rendered from."""
    return json.loads(re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)[1])


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
        _name(geo, city, o["arrivalAirport"]["city"]["name"])
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
        _name(geo, city, self.names.get(dest, dest))
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
        _name(geo, city, self.names.get(dest, dest))
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


class RouteSource:
    """A source searched route by route: subclasses list the routes and price one route per call."""

    NAME = ""
    routes: list[tuple[str, str]]

    def jobs(self, trips: list[tuple[Window, date]], geo: Geo) -> list:
        return [partial(self.offers, o, d, trips, geo) for o, d in self.routes]

    def offers(self, origin: str, dest: str, trips: list[tuple[Window, date]], geo: Geo) -> list[Offer]:
        raise NotImplementedError


class EasyJet(RouteSource):
    """easyJet: cheapest fare of each day on a route, both directions, whole season in one call. No times."""

    NAME = "easyJet"
    HOME = "https://www.easyjet.com/it/"

    def __init__(self, cfg: dict) -> None:
        self.site = _Site(self.HOME)
        valid = _next_data(self.site.page)["props"]["pageProps"]["validRoutes"]
        self.routes = [(a, r["airportCode"]) for a in cfg["airports"] for r in valid.get(a) or []
                       if not r["isWorldwide"] and re.fullmatch(r"[A-Z]{3}", r["airportCode"])]  # skip connections

    def offers(self, origin: str, dest: str, trips: list[tuple[Window, date]], geo: Geo) -> list[Offer]:
        data = self.site.json("GET", "https://www.easyjet.com/homepage/api/availability",
                              params={"origin": origin, "destination": dest, "currency": "EUR", "isReturn": "true"},
                              headers={"Accept": "application/json", "Referer": self.HOME})

        def days(flights: list | None) -> dict[str, Day]:
            return _by_day((f["date"], f.get("price"), f["date"], 0) for f in flights or [])

        return _daily(self.NAME, origin, dest, days(data.get("departureFlights")), days(data.get("returnFlights")),
                      trips, geo, _easyjet_link)


def _easyjet_link(origin: str, dest: str, out: date, back: date | None = None) -> str:
    return (f"https://www.easyjet.com/deeplink?dep={origin}&dest={dest}&dd={out}{f'&rd={back}' if back else ''}"
            f"&isOneWay={'off' if back else 'on'}&apax=1&cpax=0&ipax=0&fare=Y&lang=it")


class ITA(RouteSource):
    """ITA Airways fare calendar (same platform as Lufthansa): for each day, the cheapest round trip of a given
    length or the cheapest one-way. Every destination ITA sells from Rome, long haul and connections too.
    Prices are cached and the API gives no flight times."""

    NAME = "ITA Airways"
    API = "https://www.ita-airways.com/service/api"
    HEADERS = {"X-Portal": "ITA", "X-Portal-Language": "it", "X-Portal-Site": "IT", "Accept": "application/json"}

    def __init__(self, cfg: dict) -> None:
        finder = self._get(f"{self.API}/destinationfinder/destination/finder/{cfg['airport']}/E")
        offers = finder["destinationFinderOffers"]
        self.routes = [(cfg["airport"], o["city"]) for o in offers]
        self.stops = {o["city"]: o["numberOfStopovers"] for o in offers}
        self.names = {o["city"]: o["cityName"] for o in offers}

    def _get(self, url: str, params: dict | None = None) -> dict:
        """Behind Cloudflare, like Lufthansa. Answers 204 when there are no fares, and now and then a 500 that
        goes away when asked again."""
        for attempt in range(3):
            r = _browser().get(url, params=params, headers=self.HEADERS)
            if r.status_code == 204:
                return {}
            if r.status_code < 500 or attempt == 2:
                r.raise_for_status()
                return r.json()
            time.sleep(2 * (attempt + 1))

    def _prices(self, origin: str, dest: str, nights: int, months: list[date]) -> dict[str, float]:
        """Price per outbound date; nights 0 means one-way. A call covers 7 months from the first of the month."""
        prices: dict[str, float] = {}
        for m in months:
            start = quote(f"{m}T00:00:00+02:00", safe="")  # the API fails on UTC midnight
            try:
                data = self._get(f"{self.API}/flight-prices/{origin}/{dest}/E/{start}/{nights}",
                                 {"mode": "DAYS", "monthsToFetch": 6})
            except Exception as e:  # keep the other months
                log.info("ITA %s-%s %s: %s", origin, dest, m, e)
                continue
            if data.get("currency") == "EUR":
                prices |= {f["outboundDate"][:10]: f["price"] for f in data.get("farePerDates") or [] if f.get("price")}
        return prices

    def offers(self, origin: str, dest: str, trips: list[tuple[Window, date]], geo: Geo) -> list[Offer]:
        # A call starting in month m covers about 7 months; starts more than 6 months ahead fail
        months = [m for m in _months(trips, 6) if m <= _add_months(date.today(), 6)]
        stops = self.stops.get(dest, -1)

        def one_way(a: str, b: str) -> dict[str, Day]:
            return {d: (p, d, stops) for d, p in self._prices(a, b, 0, months).items()}

        legs = _daily(self.NAME, origin, dest, one_way(origin, dest), one_way(dest, origin), trips, geo, _ita_link,
                      self.names.get(dest, ""))
        offers = [o for o in legs if o.trip != "ar"]
        city = geo.city_of(dest)
        for nights in sorted({(w.end - d).days for w, d in trips}):
            prices = self._prices(origin, dest, nights, months)
            offers += [Offer(w, city, prices[str(d)], str(d), str(w.end), origin, dest, stops, self.NAME,
                             _ita_link(origin, dest, d, w.end))
                       for w, d in trips if (w.end - d).days == nights and str(d) in prices]
        return offers


def _ita_link(origin: str, dest: str, out: date, back: date | None = None) -> str:
    trip = f"R&ReturnDate={back}" if back else "O"
    return (f"https://www.ita-airways.com/it/it/homepage?OriginCode={origin}&DestinationCode={dest}&TripType={trip}"
            f"&DepartureDate={out}&PaxAdults=1&Cabin=E")


class Volotea(RouteSource):
    """Volotea price cache: cheapest fare of each day with its departure time, a year and both directions in
    one call."""

    NAME = "Volotea"
    API = "https://api.volotea.com/voe/price/v1/Cache/getminprice"
    APP = {"AppId": "09c45c37", "AppKey": "d0b1a5564d0f7d37baee678ea5-vocms"}  # public, from volotea.com

    def __init__(self, cfg: dict) -> None:
        stations = _get("https://json.volotea.com/dist/stations/stations.json")
        self.routes, self.names = [], {}
        for a in cfg["airports"]:
            for code, m in ((stations.get(a) or {}).get("Markets") or {}).items():
                if m.get("Enabled") and m.get("FlightType") == "Direct" and m.get("OperatingCarrier") == "V7":
                    self.routes.append((a, code))
                    self.names[code] = stations.get(code, {}).get("Culture", {}).get("it-IT", {}).get("Name", "")

    def offers(self, origin: str, dest: str, trips: list[tuple[Window, date]], geo: Geo) -> list[Offer]:
        start, end = min(d for _, d in trips), max(w.end for w, _ in trips)

        def market(a: str, b: str) -> dict:
            return {"MarketType": "SSS-SSS", "MarketValue": f"{a}-{b}", "StartDate": str(start), "EndDate": str(end),
                    "FareType": "R", "PriceWithFee": True, "GroupBy": "Date", "MaxResults": "400",
                    "RepeatMarkets": False}

        data = _get(self.API, headers={"x-api-key": self.APP["AppKey"]},
                    body=self.APP | {"RestRequest": {"Url": "markets",
                                                     "Content": [market(origin, dest), market(dest, origin)]}})
        days = {
            b["MarketValue"]: _by_day((f["DepartureFlightDate"][:10], f["Price"], f["DepartureTime"][:16], 0)
                                      for f in b.get("Value") or [])
            for b in data.get("Content") or []
        }
        return _daily(self.NAME, origin, dest, days.get(f"{origin}-{dest}", {}), days.get(f"{dest}-{origin}", {}),
                      trips, geo, _volotea_link, self.names.get(dest, ""))


def _volotea_link(origin: str, dest: str, out: date, back: date | None = None) -> str:
    return (f"https://book.volotea.com/booking/flights?culture=it-IT&from={origin}&to={dest}&departuredate={out}"
            f"{f'&returndate={back}' if back else ''}&adults=1&children=0&infants=0")


class Transavia(RouteSource):
    """Transavia fare calendar: cheapest fare of each day, three months per call. No flight times."""

    NAME = "Transavia"
    API = "https://www.transavia.com/start/api/calendar-fares"

    def __init__(self, cfg: dict) -> None:
        self.site = _Site("https://www.transavia.com/it-IT/home/")
        info = _next_data(self.site.page)["props"]["pageProps"]["pageProps"]["routeInformation"]
        self.routes = [(a, d) for a in cfg["airports"] for d in info["routes"].get(a) or {}]
        self.names = {a["code"]: a["destinationName"] for a in info["airports"]}

    def _days(self, origin: str, dest: str, months: list[date]) -> dict[str, Day]:
        fares = []
        for m in months:
            data = self.site.json("GET", self.API, headers={"Accept": "application/json"}, params={
                "dr": f"{m:%Y-%m}/{_add_months(m, 2):%Y-%m}", "ac": 1, "cc": 0, "ic": 0, "ds": origin, "as": dest,
                "lf": "Monetary"})
            fares += [(f["date"], f.get("price"), f["date"], 0) for f in data.get("data") or []]
        return _by_day(fares)

    def offers(self, origin: str, dest: str, trips: list[tuple[Window, date]], geo: Geo) -> list[Offer]:
        months = _months(trips, 3)
        return _daily(self.NAME, origin, dest, self._days(origin, dest, months), self._days(dest, origin, months),
                      trips, geo, _transavia_link, self.names.get(dest, ""))


def _transavia_link(origin: str, dest: str, out: date, back: date | None = None) -> str:
    ret = f"r=True&id={back.day}&im={back.month}&iy={back.year}" if back else "r=False"
    return (f"https://www.transavia.com/prenotare/it-it/cerca-un-volo?{ret}&ap=1&cp=0&ip=0&ds={origin}&as={dest}"
            f"&od={out.day}&om={out.month}&oy={out.year}")


class SAS(RouteSource):
    """SAS date picker: cheapest fare of each day, a month per call. No flight times or stops."""

    NAME = "SAS"
    API = "https://www.flysas.com/bff/datepicker/flights/offers/v2"

    def __init__(self, cfg: dict) -> None:
        self.routes = [(cfg["airport"], d) for d in cfg["destinations"]]

    def _days(self, origin: str, dest: str, months: list[date]) -> dict[str, Day]:
        fares = []
        for m in months:
            try:
                data = _get(self.API, {"market": "it-en", "origin": origin, "destination": dest, "adult": 1,
                                       "bookingFlow": "revenue", "tripType": "OW",
                                       "departureDate": str(max(m, date.today()))}, tries=1)
            except requests.HTTPError as e:  # some months are refused with a 400: keep the others
                if e.response is None or e.response.status_code != 400:
                    raise
                continue
            if data.get("currency") == "EUR":
                fares += [(d, f.get("totalPrice"), d, -1) for d, f in (data.get("outbound") or {}).items()]
        return _by_day(fares)

    def offers(self, origin: str, dest: str, trips: list[tuple[Window, date]], geo: Geo) -> list[Offer]:
        months = _months(trips)
        return _daily(self.NAME, origin, dest, self._days(origin, dest, months), self._days(dest, origin, months),
                      trips, geo, _sas_link)


def _sas_link(origin: str, dest: str, out: date, back: date | None = None) -> str:
    trip = f"round-trip&returnDate={back}" if back else "one-way"
    return (f"https://www.flysas.com/it-en/book-new/revenue?origin={origin}&destination={dest}&departureDate={out}"
            f"&tripType={trip}&adults=1&children=0&infants=0&youths=0")


class AirBaltic(RouteSource):
    """airBaltic fare finder: cheapest fare of each day for a year in one call, direct or via Riga. No times."""

    NAME = "airBaltic"
    API = "https://www.airbaltic.com/api"

    def __init__(self, cfg: dict) -> None:
        dests = _get(f"{self.API}/orig-dest/en")["destinData"]
        self.routes, self.names = [], {}
        for a in cfg["airports"]:
            for d in ((dests.get(f"{a}A") or {}).get("btDest") or {}).values():
                if d.get("isBTDest"):
                    self.routes.append((a, d["code"]))
                    self.names[d["code"]] = d.get("city", "")

    def _days(self, origin: str, dest: str) -> dict[str, Day]:
        data = _get(f"{self.API}/fsf/outbound", {"flightMode": "oneway", "origin": origin, "destin": dest})
        return _by_day((f["date"], f.get("price"), f["date"], 0 if f.get("isDirect") else 1)
                       for f in data.get("data") or [])

    def offers(self, origin: str, dest: str, trips: list[tuple[Window, date]], geo: Geo) -> list[Offer]:
        return _daily(self.NAME, origin, dest, self._days(origin, dest), self._days(dest, origin), trips, geo,
                      _aviasales_link, self.names.get(dest, ""))


class Pegasus(RouteSource):
    """Pegasus fare calendar: cheapest fare of each day, three months per call, to Istanbul Sabiha Gökçen and
    beyond with a change there. No flight times."""

    NAME = "Pegasus"
    HOME = "https://www.flypgs.com/en"
    HEADERS = {"X-Platform": "web", "X-Version": "1.66.0", "Origin": "https://www.flypgs.com", "Referer": HOME,
               "Accept": "application/json"}

    def __init__(self, cfg: dict) -> None:
        self.routes, self.names, self.direct = [], {}, set()
        for a in cfg["airports"]:
            for country in _get(f"https://www.flypgs.com/apint/pm/arr/en/{a}")["list"]:
                for p in country["ports"]:
                    if re.fullmatch(r"[A-Z]{3}", p["portCode"]) and (a, p["portCode"]) not in self.routes:
                        self.routes.append((a, p["portCode"]))  # skips groups like IST_SAW
                        self.names[p["portCode"]] = p["cityName"]
                        if p["isDirectFlight"]:
                            self.direct.add(p["portCode"])
        self.site = _Site(self.HOME)

    def _days(self, origin: str, dest: str, months: list[date], stops: int) -> dict[str, Day]:
        fares = []
        for m in months:
            data = self.site.json("POST", "https://www.flypgs.com/apint/cheapfare/flight-calender-prices",
                                  headers=self.HEADERS, json={"depPort": origin, "arrPort": dest,
                                                              "flightDate": str(max(m, date.today())), "currency": "EUR"})
            fares += [(d["flightDate"], d["cheapFare"]["amount"], d["flightDate"], stops)
                      for month in data.get("cheapFareFlightCalenderModelList") or [] for d in month.get("days") or []
                      if d.get("cheapFare") and d["cheapFare"].get("currency") == "EUR"]
        return _by_day(fares)

    def offers(self, origin: str, dest: str, trips: list[tuple[Window, date]], geo: Geo) -> list[Offer]:
        months, stops = _months(trips, 3), 0 if dest in self.direct else 1
        return _daily(self.NAME, origin, dest, self._days(origin, dest, months, stops),
                      self._days(dest, origin, months, stops), trips, geo, _aviasales_link, self.names.get(dest, ""))


class AirTrfx:
    """Fares travellers searched on airline websites in the last days, cached by EveryMundo airTRFX (the service
    behind the airlines' "flights from Rome" pages). Many airlines and long haul, but only the dates someone
    searched, and no flight times or stops."""

    NAME = "airTRFX"
    API = "https://openair-california.airtrfx.com/airfare-sputnik-service/v3/{}/fares/search"
    KEY = "HeQpRjsFI5xlAaSx2onkjc1HTK0ukqA1IrVvd5fvaMhNtzLTxInTpeYB1MK93pah"  # public, from the airlines' pages
    SITE = "https://www.flytap.com"  # the service only answers to airline websites
    AIRLINES = {"TK": "Turkish Airlines", "TP": "TAP Air Portugal", "VF": "AJet", "AT": "Royal Air Maroc",
                "UX": "Air Europa", "AF": "Air France", "KL": "KLM", "A3": "Aegean", "BT": "airBaltic"}

    def __init__(self, cfg: dict) -> None:
        self.airport, self.tenants = cfg["airport"], cfg["airlines"]

    def jobs(self, trips: list[tuple[Window, date]], geo: Geo) -> list:
        return [partial(self.offers, t, trips, geo) for t in self.tenants]

    def offers(self, tenant: str, trips: list[tuple[Window, date]], geo: Geo) -> list[Offer]:
        by_out: dict[str, list[Window]] = {}
        by_end: dict[str, list[Window]] = {}
        for w, d in trips:
            by_out.setdefault(str(d), []).append(w)
            if w not in by_end.get(str(w.end), []):
                by_end.setdefault(str(w.end), []).append(w)
        start, last = min(d for _, d in trips), max(w.end for w, _ in trips)
        offers = []
        while start <= last:
            end = min(start + timedelta(days=29), last)
            for journey, origins, dests in (("ROUND_TRIP", [self.airport], []), ("ONE_WAY", [self.airport], []),
                                            ("ONE_WAY", [], [self.airport])):
                fares = _get(self.API.format(tenant), headers={"em-api-key": self.KEY, "Origin": self.SITE,
                                                               "Referer": self.SITE + "/"},
                             body={"origins": origins, "destinations": dests, "journeyType": journey,
                                   "departure": {"start": str(start), "end": str(end)},
                                   "routesLimit": 100, "faresLimit": 500, "faresPerRoute": 50})
                for f in fares:
                    if f["priceSpecification"]["currencyCode"] != "EUR":
                        continue
                    a, b = f["outboundFlight"]["departureAirportIataCode"], f["outboundFlight"]["arrivalAirportIataCode"]
                    price, dep, ret = f["priceSpecification"]["totalPrice"], f["departureDate"], f.get("returnDate")
                    code = (f.get("airline") or {}).get("iataCode", tenant.upper())
                    carrier = self.AIRLINES.get(code, code)
                    if journey == "ROUND_TRIP":
                        offers += [Offer(w, geo.city_of(b), price, dep, ret, a, b, -1, carrier,
                                         _aviasales_link(a, b, dep, ret)) for w in by_out.get(dep, []) if str(w.end) == ret]
                    elif origins:
                        offers += [Offer(w, geo.city_of(b), price, dep, "", a, b, -1, carrier, _aviasales_link(a, b, dep),
                                         "andata") for w in by_out.get(dep, [])]
                    else:
                        offers += [Offer(w, geo.city_of(a), price, dep, "", a, b, -1, carrier, _aviasales_link(a, b, dep),
                                         "ritorno") for w in by_end.get(dep, [])]
            start = end + timedelta(days=1)
        return offers


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
