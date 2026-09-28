"""
Gedeelde scraping-code: Google Flights laden en de "Prijsinzichten" uitlezen
============================================================================

Gebruikt door 1_scanner.py (steekproef over alle routes) en 3_recheck.py
(hercontrole van kandidaat-deals). Alles hier is bewust gedeeld, zodat beide stappen
exact dezelfde zoekopdracht en uitleesmethode gebruiken: een hercontrole is alleen
zinvol als hij dezelfde prijs leest als de scanner.

LET OP: dit scraped Google Flights (via een echte, headless browser), wat in
strijd is met hun gebruiksvoorwaarden. Houd het volume laag en gespreid; de
instellingen hieronder zijn een bewuste afweging tussen dekking en blokkaderisico.
"""

import re
import time
from datetime import datetime

import fast_flights as ff

# ============================================================
# SCRAPING-ETIQUETTE EN SCANINSTELLINGEN
# ============================================================

# Verblijfsduur: één representatieve waarde per zoekopdracht is genoeg --
# we vertrouwen op Google's EIGEN vergelijking.
STAY_DAYS = 14

# Vertraging tussen zoekopdrachten (seconden), met jitter.
SEARCH_DELAY_MIN_SECONDS = 4.0
SEARCH_DELAY_MAX_SECONDS = 9.0

# Retries bij netwerk-/browserfouten (met toenemende wachttijd).
MAX_RETRIES = 2
RETRY_DELAY_SECONDS = 15.0

# Stop de run als zoveel zoekopdrachten achter elkaar mislukken: dan zijn we
# hoogstwaarschijnlijk geblokkeerd (captcha/rate limit) en maakt doorgaan het
# alleen erger.
MAX_CONSECUTIVE_ERRORS = 5

# Retries voor het wegschrijven naar Supabase.
DB_MAX_ATTEMPTS = 3
DB_RETRY_DELAY_SECONDS = 3.0


# ============================================================
# BROWSER-INTERACTIE
# ============================================================

def build_search_url(origin, destination, depart_date, return_date, seat="economy"):
    query = ff.create_query(
        flights=[
            ff.FlightQuery(date=depart_date, from_airport=origin, to_airport=destination),
            ff.FlightQuery(date=return_date, from_airport=destination, to_airport=origin),
        ],
        trip="round-trip",
        seat=seat,
        passengers=ff.Passengers(adults=1),
        language="nl",  # de regexes hieronder verwachten Nederlandse paginatekst
        currency="EUR",
    )
    return query.url()


def accept_consent_if_present(page):
    """Klikt op 'Alles accepteren' als de EU-cookiebanner verschijnt."""
    for tekst in ["Alles accepteren", "Accept all", "Alle accepteren"]:
        try:
            knop = page.get_by_role("button", name=tekst, exact=False)
            if knop.count() > 0 and knop.first.is_visible():
                knop.first.click(timeout=5000)
                page.wait_for_timeout(1000)
                return True
        except Exception:
            continue
    return False


# ============================================================
# PRIJSINZICHTEN UITLEZEN
# ============================================================

def _parse_bedrag(tekst):
    """'1.234,56' -> 1234.56 ; None bij een onleesbaar bedrag."""
    try:
        return float(tekst.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def extract_price_insights(full_text):
    """
    Haalt uit de (platte) paginatekst van de EERSTE resultatenpagina:
    - de laagste prijs die Google toont ('vanaf € X' of de eerste prijs)
    - het kwalitatieve label uit 'Prijsinzichten' (laag/gemiddeld/hoog)
    - de genoemde ondergrens ('onwaarschijnlijk dat de prijzen dalen tot onder € X')
    """
    resultaat = {
        "laagste_prijs": None,
        "insight_label": None,
        "prijs_ondergrens": None,
    }

    # Laagste prijs: meestal "vanaf € X" bij het 'Goedkoopst'-tabblad
    m = re.search(r"vanaf\s*€\s?([\d.,]+)", full_text, re.IGNORECASE)
    if not m:
        # Terugval: de eerste prijs die in de resultatenlijst voorkomt
        m = re.search(r"€\s?([\d.,]+)", full_text)
    if m:
        resultaat["laagste_prijs"] = _parse_bedrag(m.group(1))

    # Kwalitatief label: "De prijzen zijn momenteel laag/gemiddeld/hoog"
    m = re.search(r"[Dd]e prijzen zijn (?:momenteel|nu)\s+(laag|gemiddeld|hoog)", full_text)
    if m:
        resultaat["insight_label"] = m.group(1).lower()
    else:
        # Alternatieve formulering: "€ 479 is laag voor Economy"
        m = re.search(r"€\s?[\d.,]+\s+is\s+(laag|gemiddeld|hoog)", full_text, re.IGNORECASE)
        if m:
            resultaat["insight_label"] = m.group(1).lower()

    # Ondergrens: "onwaarschijnlijk dat de prijzen dalen tot onder € X"
    m = re.search(
        r"onwaarschijnlijk dat (?:de|deze) prij(?:zen|s) (?:dalen|daalt)\s*tot onder\s*€\s?([\d.,]+)",
        full_text,
    )
    if m:
        resultaat["prijs_ondergrens"] = _parse_bedrag(m.group(1))

    return resultaat


def _naar_datetime(simple_datetime):
    jaar, maand, dag = simple_datetime.date
    uur, minuut = simple_datetime.time
    return datetime(jaar, maand, dag, uur, minuut)


def extract_outbound_itinerary(html):
    """
    Haalt via fast_flights' EIGEN parser (dezelfde paginalading, geen extra request)
    het aantal tussenstops, de tussenstop-luchthavens en de reisduur van de
    HEENreis. Alleen de heenreis: Google toont een retour in twee stappen (heen
    kiezen, dan pas terug) en we klikken niet door (zou een 2e paginalading per
    combinatie betekenen). fast_flights sorteert de itineraries op prijs, dus
    entry 0 hoort bij dezelfde laagste prijs als extract_price_insights vindt.

    Best-effort: geeft None bij een lege/onverwachte pagina (bijv. geen resultaten,
    of een paginastructuur die de parser niet herkent) -- dat laat de rest van de
    scan (prijs/label) onaangetast.
    """
    try:
        resultaten = ff.parser.parse(html)
    except Exception:
        return None
    if not resultaten:
        return None

    legs = resultaten[0].flights
    if not legs:
        return None

    vertrek = _naar_datetime(legs[0].departure)
    aankomst = _naar_datetime(legs[-1].arrival)

    return {
        "stops": len(legs) - 1,
        "stopover_airports": [leg.to_airport.code for leg in legs[:-1]],
        "duration_minutes": round((aankomst - vertrek).total_seconds() / 60),
    }


def search_route_insights(page, origin, destination, depart_date, return_date, seat="economy"):
    """
    Doet 1 zoekopdracht en haalt de prijsinzichten van de EERSTE resultaten-
    pagina op (geen doorklikken naar boekingsopties), plus (uit dezelfde
    paginalading) tussenstops/reisduur van de heenreis. Geeft None terug als
    het na alle retries nog steeds mislukt.
    """
    url = build_search_url(origin, destination, depart_date, return_date, seat=seat)

    for poging in range(1, MAX_RETRIES + 2):
        try:
            _t0 = time.monotonic()
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            _t_goto = time.monotonic()
            page.wait_for_timeout(1500)
            accept_consent_if_present(page)
            _t_consent = time.monotonic()

            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                pass
            _t_idle = time.monotonic()

            try:
                page.wait_for_selector("text=/€\\s?\\d/", timeout=30000)
            except Exception:
                print(f"    WAARSCHUWING: geen prijzen gevonden binnen 30s voor {origin}->{destination} {depart_date}.")
            _t_selector = time.monotonic()

            page.wait_for_timeout(4000)
            print(f"    [TIMING] goto={_t_goto-_t0:.1f}s consent_wait={_t_consent-_t_goto:.1f}s "
                  f"networkidle={_t_idle-_t_consent:.1f}s selector={_t_selector-_t_idle:.1f}s "
                  f"totaal_tot_hier={_t_selector-_t0:.1f}s")

            full_text = page.inner_text("body")
            resultaat = extract_price_insights(full_text)

            itinerary = extract_outbound_itinerary(page.content())
            resultaat["outbound_stops"] = itinerary["stops"] if itinerary else None
            resultaat["outbound_stopover_airports"] = itinerary["stopover_airports"] if itinerary else None
            resultaat["outbound_duration_minutes"] = itinerary["duration_minutes"] if itinerary else None

            return resultaat

        except Exception as e:
            print(f"    FOUT (poging {poging}/{MAX_RETRIES + 1}): {type(e).__name__}: {e}")
            if poging <= MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS * poging)  # backoff: 15s, 30s, ...

    return None


def save_scan(client, row):
    """Schrijft 1 scanrij weg, met een paar retries bij tijdelijke DB-/netwerkfouten."""
    for poging in range(1, DB_MAX_ATTEMPTS + 1):
        try:
            client.table("scans").insert(row).execute()
            return True
        except Exception as e:
            print(f"    DB-FOUT (poging {poging}/{DB_MAX_ATTEMPTS}): {type(e).__name__}: {e}")
            if poging < DB_MAX_ATTEMPTS:
                time.sleep(DB_RETRY_DELAY_SECONDS * poging)
    return False


def build_scan_row(route_id, depart_date, return_date, insights, cabin_class="economy"):
    """Zet het resultaat van search_route_insights om naar een rij voor `scans`."""
    if insights is None:
        return {
            "route_id": route_id, "depart_date": depart_date, "return_date": return_date,
            "stay_days": STAY_DAYS, "lowest_price": None, "insight_label": "fout",
            "price_floor": None, "is_deal": False, "cabin_class": cabin_class,
            "outbound_stops": None, "outbound_stopover_airports": None,
            "outbound_duration_minutes": None,
        }

    heeft_prijs = insights["laagste_prijs"] is not None
    if insights["insight_label"]:
        label = insights["insight_label"]
    else:
        label = "onbekend" if heeft_prijs else "geen_data"

    return {
        "route_id": route_id, "depart_date": depart_date, "return_date": return_date,
        "stay_days": STAY_DAYS,
        "lowest_price": insights["laagste_prijs"],
        "insight_label": label,
        "price_floor": insights["prijs_ondergrens"],
        "is_deal": label == "laag",
        "cabin_class": cabin_class,
        "outbound_stops": insights.get("outbound_stops"),
        "outbound_stopover_airports": insights.get("outbound_stopover_airports"),
        "outbound_duration_minutes": insights.get("outbound_duration_minutes"),
    }

