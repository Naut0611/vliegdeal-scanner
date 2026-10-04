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

import json
import random
import re
import time
from datetime import datetime, timedelta

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
    het aantal tussenstops, de tussenstop-luchthavens, de reisduur en de maatschappij(en)
    van de HEENreis. Alleen de heenreis: Google toont een retour in twee stappen (heen
    kiezen, dan pas terug) en we klikken niet door (zou een 2e paginalading per
    combinatie betekenen). fast_flights sorteert de itineraries op prijs, dus
    entry 0 hoort bij dezelfde laagste prijs als extract_price_insights vindt.

    Maatschappij: fast_flights' parser geeft dit al mee (Flights.airlines) uit dezelfde
    paginalading -- geen aparte request. De waarden zijn soms al namen ('Vueling'), soms
    IATA-codes ('VY'); resultaten.metadata.airlines (code -> naam, uit dezelfde pagina)
    lost dat laatste op. Onbekende/al-herkenbare waarden vallen terug op zichzelf.

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

    beste = resultaten[0]
    legs = beste.flights
    if not legs:
        return None

    vertrek = _naar_datetime(legs[0].departure)
    aankomst = _naar_datetime(legs[-1].arrival)

    metadata = getattr(resultaten, "metadata", None)
    naam_per_code = {a.code: a.name for a in metadata.airlines} if metadata else {}
    airlines = []
    for waarde in beste.airlines or []:
        naam = naam_per_code.get(waarde, waarde)
        if naam not in airlines:
            airlines.append(naam)

    return {
        "stops": len(legs) - 1,
        "stopover_airports": [leg.to_airport.code for leg in legs[:-1]],
        "duration_minutes": round((aankomst - vertrek).total_seconds() / 60),
        "airlines": airlines,
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
            resultaat["outbound_airlines"] = itinerary["airlines"] if itinerary else None

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
            "outbound_duration_minutes": None, "outbound_airlines": None,
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
        "outbound_airlines": insights.get("outbound_airlines"),
    }


def save_scans_bulk(client, rows):
    """Als save_scan, maar voor meerdere rijen in 1 databaseverzoek (zie build_calendar_scan_rows
    hieronder: één kalenderraster levert tot ~60 rijen tegelijk op -- 60 losse inserts zou onnodig
    veel databaseverkeer geven voor iets dat niets met scraping-etiquette te maken heeft)."""
    if not rows:
        return True
    for poging in range(1, DB_MAX_ATTEMPTS + 1):
        try:
            client.table("scans").insert(rows).execute()
            return True
        except Exception as e:
            print(f"    DB-FOUT bulk-insert (poging {poging}/{DB_MAX_ATTEMPTS}): {type(e).__name__}: {e}")
            if poging < DB_MAX_ATTEMPTS:
                time.sleep(DB_RETRY_DELAY_SECONDS * poging)
    return False


# ============================================================
# KALENDERRASTER -- alternatief voor een vaste steekproefdatum (zie 1_scanner.py)
# ============================================================
#
# Google Flights doet, zodra je op het datumveld klikt, zelf een achtergrondverzoek naar
# 'GetCalendarPicker' -- hetzelfde verzoek dat een bezoeker triggert door handmatig op de datum te
# klikken. Dat antwoord bevat ~60 opeenvolgende ECHTE dagprijzen (voor een vaste verblijfsduur) rond
# de opgegeven datum, in 1 extra achtergrondverzoek bovenop de resultatenpagina die er toch al
# geladen wordt. Daarmee kunnen we de daadwerkelijk goedkoopste dag in een maand VINDEN, in plaats
# van te gokken met een vast, willekeurig verspreid steekproefdatum (zie generate_sample_dates in
# 1_scanner.py) en te hopen dat die toevallig goed uitpakt.
#
# Twee kanttekeningen, bewust zo geaccepteerd (zie CLAUDE.md):
# - GetCalendarPicker is een niet-gedocumenteerd, intern Google-endpoint en de klik op het datumveld
#   gebeurt op een vaste pixelpositie (_KALENDER_KLIK_X/_Y, zie CALENDAR_VIEWPORT hieronder) omdat
#   tekst-/rol-gebaseerde locators bleken te botsen met verborgen toegankelijkheids-duplicaten van
#   dezelfde datumtekst elders op de pagina. Kan zonder aankondiging stuk gaan bij een wijziging aan
#   Google's kant -- vandaar dat 1_scanner.py hier altijd op terugvalt naar de oude, directe aanpak
#   (fetch_calendar_prices geeft dan None terug i.p.v. een fout op te werpen).
# - Voor een dunbevolkte route (weinig vluchtaanbod) geeft Google soms maar een handvol dagen terug
#   i.p.v. het volledige raster (~60 dagen) -- MIN_CALENDAR_DAYS is de ondergrens waaronder we dat
#   als 'te weinig data' behandelen en dus ook terugvallen op de oude aanpak.

# Vast venstergrootte waarop _KALENDER_KLIK_X/_Y is bepaald; 1_scanner.py moet de pagina met exact
# deze viewport openen.
CALENDAR_VIEWPORT = {"width": 1400, "height": 1000}
_KALENDER_KLIK_X, _KALENDER_KLIK_Y = 922, 163
# Retourveld, direct rechts van het vertrekveld op dezelfde hoogte (zelfde vaste-pixelpositie-aanpak
# en dezelfde kanttekeningen als _KALENDER_KLIK_X/_Y hierboven) -- zie fetch_return_date_prices().
_RETOUR_KLIK_X, _RETOUR_KLIK_Y = 1091, 163
# "Volgende"-pijl rechts van de twee getoonde kalendermaanden (zelfde vaste-pixelpositie-aanpak en
# kanttekeningen als hierboven): elke klik schuift één maand op en triggert een EIGEN
# GetCalendarPicker-verzoek voor die ene maand -- zie fetch_calendar_prices(months=...).
_VOLGENDE_MAAND_KLIK_X, _VOLGENDE_MAAND_KLIK_Y = 1227, 414
# Het eerste raster toont al twee maanden (huidige + volgende), elke extra klik komt er één bij.
_MAANDEN_IN_EERSTE_RASTER = 2
_GET_CALENDAR_PICKER = "GetCalendarPicker"
MIN_CALENDAR_DAYS = 30
# Het retourraster is van zichzelf al klein (Google toont maar een beperkt aantal nachten rond de
# opgegeven verblijfsduur, geen los-van-de-werkelijkheid lange periode) -- een lagere ondergrens dan
# MIN_CALENDAR_DAYS is dus terecht, niet een teken van een mislukte/onvolledige response.
MIN_RETURN_DAYS = 10


def _chunks_uit_calendar_response(raw_text):
    """
    Splitst Google's 'batchexecute'-envelope (')]}'' + lengte-geprefixte JSON-regels) in de losse
    JSON-chunks. De opgegeven lengte-prefix bleek niet exact overeen te komen met de regel-lengte
    (een paar bytes verschil, vermoedelijk regeleinde-normalisatie) -- daarom simpelweg op regels
    splitsen i.p.v. op de opgegeven bytelengte vertrouwen: elke chunk staat toch al op zijn eigen
    regel, direct na een regel die alleen een getal bevat.
    """
    regels = raw_text.split("\n")
    chunks, i = [], 0
    while i < len(regels):
        if re.fullmatch(r"\d+", regels[i] or ""):
            if i + 1 < len(regels):
                chunks.append(regels[i + 1])
            i += 2
        else:
            i += 1
    return chunks


def parse_calendar_picker_response(raw_text):
    """
    Geeft een lijst dicts {depart_date, return_date, price} terug uit de ruwe
    GetCalendarPicker-response. Gooit een fout als de envelope niet herkend wordt (bijv. omdat
    Google 'm heeft gewijzigd) -- de aanroeper (fetch_calendar_prices) vangt dat op en valt terug
    op de oude, directe aanpak i.p.v. dat de hele run hierop vastloopt.
    """
    if not raw_text.startswith(")]}'"):
        raise ValueError("Onverwachte response-vorm: verwachtte Google's \")]}'\"-envelope.")
    chunks = _chunks_uit_calendar_response(raw_text)
    if not chunks:
        raise ValueError("Geen JSON-chunk gevonden in de response.")
    buiten = json.loads(chunks[0])
    # buiten = [["wrb.fr", null, "<geneste JSON-string>", ...]]
    binnen = json.loads(buiten[0][2])
    # binnen[1] = lijst van [depart_date, return_date, [[null, prijs], opaque_token], vlag]. Bij een
    # dunbevolkte (vaak langeafstands-)combinatie ontbreekt prijsinfo voor sommige dagen (geen
    # vluchtcombinatie beschikbaar voor die specifieke dagcombinatie) -- zo'n dag slaan we gewoon
    # over i.p.v. de HELE respons te laten mislukken op één ontbrekende prijs.
    dagen = []
    for entry in binnen[1]:
        depart_date, return_date, prijsinfo, *_ = entry
        if not prijsinfo or not prijsinfo[0]:
            continue
        dagen.append({"depart_date": depart_date, "return_date": return_date, "price": prijsinfo[0][1]})
    return dagen


def fetch_calendar_prices(page, origin, destination, depart_date, stay_days, cabin_class="economy", months=1):
    """
    Laadt de resultatenpagina voor (depart_date -> depart_date + stay_days) en klikt daarna het
    datumveld open, wat Google's 'GetCalendarPicker'-achtergrondverzoek triggert. Geeft een lijst
    dagprijzen terug (zie parse_calendar_picker_response), of None als dat verzoek niet gezien werd,
    te weinig dagen opleverde (< MIN_CALENDAR_DAYS, zie hierboven) of niet te parsen was -- de
    aanroeper valt dan terug op de oude, directe aanpak. Gebruikt de gegeven `page` (dezelfde
    browsersessie als de rest van de run, i.p.v. een nieuwe sessie op te zetten).

    months=1 (standaard): alleen het eerste raster (~60 dagen, vanaf vandaag). months>1: daarna
    doorklikken met de 'volgende maand'-pijl van de kalender -- precies wat een bezoeker doet --
    tot `months` maanden gedekt zijn (het eerste raster telt voor twee). Elke klik is één extra
    achtergrondverzoek op dezelfde pagina (geen nieuwe paginalading), met een korte pauze ertussen.
    Het doorklikken is best-effort: komt er na een klik geen (parsebaar) antwoord, dan stoppen we en
    geven we terug wat er tot dan toe is; het EERSTE raster bepaalt of het geheel slaagt.
    """
    return_date = (datetime.strptime(depart_date, "%Y-%m-%d") + timedelta(days=stay_days)).strftime("%Y-%m-%d")
    url = build_search_url(origin, destination, depart_date, return_date, seat=cabin_class)

    gevangen = []

    def on_response(resp):
        if _GET_CALENDAR_PICKER in resp.url:
            gevangen.append(resp)

    page.on("response", on_response)
    try:
        page.goto(url, timeout=45000, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        accept_consent_if_present(page)
        page.wait_for_timeout(1500)

        page.mouse.click(_KALENDER_KLIK_X, _KALENDER_KLIK_Y)
        page.wait_for_timeout(3000)

        if not gevangen:
            return None
        eerste = parse_calendar_picker_response(gevangen[0].text())
        if len(eerste) < MIN_CALENDAR_DAYS:
            return None
        per_dag = {d["depart_date"]: d for d in eerste}

        verwerkt = 1
        for _ in range(max(0, months - _MAANDEN_IN_EERSTE_RASTER)):
            page.wait_for_timeout(random.randint(1500, 3000))
            page.mouse.click(_VOLGENDE_MAAND_KLIK_X, _VOLGENDE_MAAND_KLIK_Y)
            page.wait_for_timeout(2500)
            if len(gevangen) <= verwerkt:
                break  # geen nieuw verzoek na de klik: pijl niet gevonden of einde van het venster
            try:
                for d in parse_calendar_picker_response(gevangen[verwerkt].text()):
                    per_dag[d["depart_date"]] = d
            except Exception:
                break
            verwerkt += 1
        return sorted(per_dag.values(), key=lambda d: d["depart_date"])
    except Exception as e:
        print(f"    kalenderraster mislukt ({type(e).__name__}: {e}), val terug op vaste datum.")
        return None
    finally:
        page.remove_listener("response", on_response)


def fetch_return_date_prices(page, origin, destination, depart_date, stay_days=STAY_DAYS, cabin_class="economy"):
    """
    Tweede stap na fetch_calendar_prices(): nu het VERTREKpunt al vastligt (de daadwerkelijk
    goedkoopste dag uit het eerste raster), hier de daadwerkelijk goedkoopste RETOURdatum zoeken
    i.p.v. klakkeloos een vaste verblijfsduur aanhouden. Laadt de resultatenpagina voor (depart_date
    -> depart_date + stay_days) en klikt daarna het RETOURveld open (i.p.v. het vertrekveld) --
    triggert hetzelfde 'GetCalendarPicker'-verzoek, nu met vast vertrekpunt en variabele retourdatum.
    Het venster dat Google teruggeeft wordt breder naarmate `stay_days` groter is (empirisch bepaald,
    geen officieel gedocumenteerd gedrag) -- lib.planner.stay_bounds() kiest daarom `stay_days` als
    het midden van de gewenste verblijfsduur-spreiding voor dit bestemmingstype, en filter_by_stay_
    length() dwingt daarna de daadwerkelijke min/max-grenzen af (Google's venster is een handig
    uitgangspunt, maar geen garantie dat het precies samenvalt met wat wij willen).

    Kost 1 extra paginalading t.o.v. alleen fetch_calendar_prices (zie CLAUDE.md): een bewuste
    afweging voor een echt geoptimaliseerde retourdatum i.p.v. een vaste aanname.
    """
    return_date = (datetime.strptime(depart_date, "%Y-%m-%d") + timedelta(days=stay_days)).strftime("%Y-%m-%d")
    url = build_search_url(origin, destination, depart_date, return_date, seat=cabin_class)

    gevangen = []

    def on_response(resp):
        if _GET_CALENDAR_PICKER in resp.url:
            gevangen.append(resp)

    page.on("response", on_response)
    try:
        page.goto(url, timeout=45000, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        accept_consent_if_present(page)
        page.wait_for_timeout(1500)

        page.mouse.click(_RETOUR_KLIK_X, _RETOUR_KLIK_Y)
        page.wait_for_timeout(3000)

        if not gevangen:
            return None
        dagen = parse_calendar_picker_response(gevangen[0].text())
        if len(dagen) < MIN_RETURN_DAYS:
            return None
        return dagen
    except Exception as e:
        print(f"    retourraster mislukt ({type(e).__name__}: {e}), behoud vaste verblijfsduur.")
        return None
    finally:
        page.remove_listener("response", on_response)


def filter_by_stay_length(dagen, depart_date, min_nachten, max_nachten):
    """
    Beperkt het retourraster tot verblijfsduren binnen [min_nachten, max_nachten] (zie
    Combo.min_stay_days/max_stay_days/lib.planner.stay_bounds()): een stedentrip en een verre reis
    hebben een compleet andere zinvolle lengte, dus 'goedkoopste retourdatum' betekent niet
    'absoluut goedkoopste optie in het hele raster' maar 'goedkoopste BINNEN een passende
    verblijfsduur voor dit bestemmingstype'.
    """
    vertrek = datetime.strptime(depart_date, "%Y-%m-%d")
    return [
        d for d in dagen
        if min_nachten <= (datetime.strptime(d["return_date"], "%Y-%m-%d") - vertrek).days <= max_nachten
    ]


def select_cheapest_return(dagen):
    """
    Geeft de dag met de laagste prijs terug (bij een gelijke prijs: de vroegste retourdatum). Geen
    maand-restrictie zoals select_cheapest_in_month(): de aanroeper filtert hiervoor al op een
    zinvolle verblijfsduur (zie filter_by_stay_length hierboven), dus elke overgebleven optie is per
    definitie geschikt -- deze functie hoeft alleen nog de goedkoopste te kiezen.
    """
    return min(dagen, key=lambda d: (d["price"], d["return_date"]))


def select_cheapest_in_month(dagen, anchor_date):
    """
    Geeft de dag met de laagste prijs terug UIT DEZELFDE KALENDERMAAND als anchor_date (bij een
    gelijke prijs: de vroegste datum). Blijft zo dicht bij wat 1_scanner.py's vaste steekproef ook
    al deed (één representatieve dag per maand) -- alleen is dat nu de daadwerkelijk goedkoopste dag
    van die maand i.p.v. een vast, willekeurig verspreid steekproefdatum. Valt terug op de
    goedkoopste dag van het HELE venster als er (onverwacht) geen enkele dag in diezelfde maand zit.
    """
    maand = anchor_date[:7]  # 'YYYY-MM'
    kandidaten = [d for d in dagen if d["depart_date"][:7] == maand] or dagen
    return min(kandidaten, key=lambda d: (d["price"], d["depart_date"]))


def select_notable_months(dagen, anchor_date, min_date, min_discount, max_n):
    """
    Voor een meermaands-raster (fetch_calendar_prices(months>1)): de goedkoopste dag van elke
    ANDERE maand dan die van anchor_date (die krijgt al de gewone behandeling) waarvan die prijs
    minstens `min_discount` (0.2 = 20%) onder de mediaan van alle (beschikbare) dagprijzen in het
    raster ligt -- een maand die dus echt opvalt en een volledige scan (Prijsinzichten-label)
    verdient, ook al is die niet aan de beurt. Alleen dagen vanaf `min_date` ('YYYY-MM-DD') tellen
    mee (niets zoeken dichter bij vandaag dan de scanner toestaat). Hoogstens `max_n`, grootste
    korting eerst; [] bij te weinig dagen om een mediaan van te nemen.
    """
    beschikbaar = [d for d in dagen if d["depart_date"] >= min_date]
    if len(beschikbaar) < MIN_CALENDAR_DAYS:
        return []
    prijzen = sorted(d["price"] for d in beschikbaar)
    mediaan = prijzen[len(prijzen) // 2] if len(prijzen) % 2 else (
        prijzen[len(prijzen) // 2 - 1] + prijzen[len(prijzen) // 2]) / 2
    grens = mediaan * (1 - min_discount)

    per_maand = {}
    for d in beschikbaar:
        maand = d["depart_date"][:7]
        if maand == anchor_date[:7]:
            continue
        if maand not in per_maand or (d["price"], d["depart_date"]) < (per_maand[maand]["price"], per_maand[maand]["depart_date"]):
            per_maand[maand] = d
    opvallend = [d for d in per_maand.values() if d["price"] <= grens]
    opvallend.sort(key=lambda d: (d["price"], d["depart_date"]))
    return opvallend[:max_n]


def build_calendar_scan_rows(route_id, dagen, cabin_class):
    """
    Zet de kalenderraster-dagprijzen om naar kale 'scans'-rijen (prijs, geen label/heenreisdetails
    -- die kent Google's kalenderraster niet, alleen de losse resultatenpagina). Puur geschiedenis:
    is_deal staat altijd op False (geen label = geen kandidaat, zie 2_curate.py), maar de prijzen
    tellen wel mee in route_medians() se discount_pct-berekening -- dus meer, snellere historie dan
    voorheen, als bijeffect van het kalenderraster.
    """
    rijen = []
    for d in dagen:
        stay_days = (datetime.strptime(d["return_date"], "%Y-%m-%d") - datetime.strptime(d["depart_date"], "%Y-%m-%d")).days
        rijen.append({
            "route_id": route_id, "depart_date": d["depart_date"], "return_date": d["return_date"],
            "stay_days": stay_days, "lowest_price": d["price"], "insight_label": None,
            "price_floor": None, "is_deal": False, "cabin_class": cabin_class,
            "outbound_stops": None, "outbound_stopover_airports": None,
            "outbound_duration_minutes": None, "outbound_airlines": None,
        })
    return rijen

