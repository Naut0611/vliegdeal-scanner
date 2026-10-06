"""
Scanner -- Google Flights "Prijsinzichten" -> tabel `scans` in Supabase
========================================================================

Dit is de public-repo kopie van de private repo's `analytics/1_scanner.py`
(zelfde logica, andere locatie): scraping-code staat bewust in een public
repo zodat de nachtelijke run gratis, onbeperkt op GitHub Actions kan draaien
(zie CLAUDE.md in de private repo voor de achtergrond van die split). De
private repo blijft leidend voor de architectuurdocumentatie; wijzigingen
hier horen ook daar (of andersom) doorgevoerd te worden.

Kernidee, ONGEWIJZIGD t.o.v. de vorige versie van dit script: we lezen Google
Flights' EIGEN ingebouwde vergelijking uit, het "Prijsinzichten"-blok
(laag/gemiddeld/hoog), i.p.v. zelf een prijs-baseline te bouwen.

WAT IS NIEUW: in plaats van direct te scannen op een vast, willekeurig verspreid
steekproefdatum (de oude generate_sample_dates-aanpak), doet deze versie eerst een
KALENDERRASTER-check (lib/scraper.fetch_calendar_prices): 1 extra achtergrondverzoek
bovenop de toch al geladen pagina, dat Google's eigen 'GetCalendarPicker' aanroept en
~60 ECHTE dagprijzen teruggeeft. Daarmee kiezen we de daadwerkelijk goedkoopste dag
BINNEN dezelfde maand als het geplande steekproefdatum, i.p.v. te gokken. Die dag wordt
vervolgens op de gebruikelijke manier volledig gescand (Prijsinzichten-label +
tussenstops/reisduur/maatschappij van de heenreis, exact zoals voorheen) -- dus geen
functionaliteit verloren, alleen een betere gekozen datum.

OOK NIEUW: het vertrekraster loopt niet meer over één, maar over alle MONTHS_AHEAD maanden: na het
eerste raster (huidige + volgende maand) klikt lib/scraper.fetch_calendar_prices met de 'volgende
maand'-pijl van de kalender door -- elke klik is één extra achtergrondverzoek op dezelfde pagina, geen
nieuwe paginalading. Het raster wordt per run per (route, cabin class) gecachet: een latere combinatie
van dezelfde route kiest de goedkoopste dag van ZIJN maand daaruit. Een ANDERE maand die daarin
opvallend goedkoop is (>= OPVALLEND_KORTING onder de rastermediaan, max. MAX_OPVALLENDE_MAANDEN_PER_ROUTE)
krijgt meteen een volledige scan, zodat een deal in bv. maand 4 niet weken op zijn beurt wacht.

OOK NIEUW: combinaties die aansluiten bij een actuele abonnee-voorkeur (tabel `subscribers`:
vertrekpunt/bestemmingsland(en)/reisperiode, zelfde matchlogica als de private repo's
send_newsletter.py via lib/subscriber_match.py) krijgen voorrang bij een beperkt budget (zie
maak_subscriber_prioriteit_fn en lib/planner.py's prioriteit_fn-parameter) -- daar weten we
aantoonbare interesse in, de rest komt daarna aan de beurt. Geen gewicht naar AANTAL matchende
abonnees: dat zou een populaire voorkeur nog verder bevoordelen t.o.v. een enkele abonnee met
een nichewens.

OOK NIEUW: na het vertrekraster hierboven volgt een TWEEDE raster-poging
(lib/scraper.fetch_return_date_prices) die, met die goedkoopste vertrekdag nu vast, Google's
retourveld opent i.p.v. het vertrekveld -- hetzelfde 'GetCalendarPicker'-verzoek, nu met vast
vertrekpunt en variabele retourdatum. Zo wordt niet langer klakkeloos een vaste 14-daagse
verblijfsduur aangenomen, maar ook de daadwerkelijk goedkoopste retourdatum gezocht. Wat een
'zinvolle' verblijfsduur is, verschilt sterk per bestemmingstype (een stedentrip naar Barcelona
mag best 2-3 dagen zijn, een verre reis naar Sydney niet, maar San Francisco -- ondanks de
vliegafstand -- qua karakter weer wel) -- zie lib.planner.stay_bounds() en destinations.csv's
'stedentrip'-tag. Kost 1 extra paginalading per combinatie waarvoor het vertrekraster al
slaagde (zie CLAUDE.md in de private repo voor de etiquette-afweging).

De losse dagprijzen uit beide rasters worden ook zelf weggeschreven (kaal: alleen
prijs, geen label/heenreis -- dat kent het raster niet) als extra, snellere scangeschiedenis
voor 2_curate.py's discount_pct-mediaan. Binnen één run wordt het vertrekraster (en het daarop
volgende retourraster) van eenzelfde (route, cabin class) maar 1x opgehaald (RASTER_AL_OPGEHAALD
hieronder) -- bij tier 1 (2 steekproefdata/maand) zou een tweede keer grotendeels dezelfde ~60
dagen opnieuw ophalen, puur dubbel werk.

TERUGVAL: is er voor een route geen (bruikbaar) kalenderraster -- te weinig vluchtaanbod
(zie lib/scraper.MIN_CALENDAR_DAYS), een netwerkfout, of Google's paginastructuur is
gewijzigd -- dan scant deze versie gewoon de oorspronkelijk geplande datum direct, exact
zoals de vorige versie van dit script altijd deed. Geen enkele bestemming valt hierdoor
buiten de boot.

Elke combinatie levert zo 1 (volledig) + tot ~60 (kale) rijen in de tabel `scans` op. Er
zijn ~400 bestemmingen (destinations.csv), veel te veel voor één run: lib/planner.py
kiest per run de meest achterstallige combinaties, tot `--budget` scans (populaire
bestemmingen vaker, obscure zeldzamer) -- deze planning-laag is ONGEWIJZIGD, alleen HOE
een gekozen combinatie wordt afgehandeld is anders. Deze stap publiceert zelf NIETS: de
private repo's 2_curate.py, 3_recheck.py en de menselijke review (4_review.py) bepalen
wat er op de website komt.

Naast Economy scannen we ook Business Class, voor alle intercontinentale bestemmingen
(destinations.land niet in lib/regions.EUROPE) -- bewust, met dezelfde tier-cadans als
hun Economy-scan; dit verhoogt het scanvolume aanzienlijk (zie CLAUDE.md).

LET OP: dit scraped Google Flights (via een echte, headless browser), wat in strijd is
met hun gebruiksvoorwaarden. Gebruik met mate: spreid zoekopdrachten over tijd, draai
NOOIT meerdere scanners tegelijk (dit script bewaakt dat zelf niet, zie lib/scanrun.py's
toelichting -- scripts/run_daily.sh is de enige lock-houder), en behandel de instellingen
als een bewuste afweging tussen dekking en risico op blokkade (vertraging en retries
staan in lib/scraper.py). Het kalenderraster leunt op een niet-gedocumenteerd, intern
Google-endpoint (zie lib/scraper.py's toelichting bij CALENDAR_VIEWPORT) -- kan zonder
aankondiging veranderen, vandaar de terugval hierboven.

Gebruik (vanuit de projectroot van DEZE repo):
    python scanner.py                 # normale run (SCANS_PER_RUN scans), schrijft naar Supabase
    python scanner.py --budget 50     # kleinere run
    python scanner.py --limit 3       # stop na 3 combinaties (om te testen)
    python scanner.py --dest BKK,DPS  # alleen deze bestemmingen (IATA), bijv. gerichte herscan
    python scanner.py --dry-run       # scrapen zonder iets naar Supabase te schrijven
"""

import argparse
import random
import time
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright

from lib.db import fetch_all
from lib.destinations import load_destinations
from lib.planner import TIER_CONFIG, Combo, select_due_combos, stay_bounds, steady_state_load
from lib.regions import is_europe
from lib.subscriber_match import matches_countries, matches_origin, matches_period
from lib.scraper import (
    CALENDAR_VIEWPORT,
    MAX_CONSECUTIVE_ERRORS,
    SEARCH_DELAY_MAX_SECONDS,
    SEARCH_DELAY_MIN_SECONDS,
    STAY_DAYS,
    build_calendar_scan_rows,
    build_scan_row,
    fetch_calendar_prices,
    fetch_return_date_prices,
    filter_by_stay_length,
    save_scan,
    save_scans_bulk,
    search_route_insights,
    select_cheapest_in_month,
    select_cheapest_return,
    select_notable_months,
)

# ============================================================
# CONFIGURATIE -- dit is de plek om het scanbereik aan te passen
# ============================================================

# Vertrekluchthavens (IATA-codes). Begin klein, brei uit zodra je vertrouwen
# hebt in de stabiliteit en het tempo.
ORIGIN_AIRPORTS = [
    "AMS",  # Amsterdam Schiphol
    "BRU",  # Brussel
    "DUS",  # Düsseldorf
    "RTM",  # Rotterdam The Hague
    "EIN",  # Eindhoven
    # Later uit te breiden met o.a.: CRL, CGN, FRA, MUC, HAM, ...
]

# RTM en EIN vliegen in de praktijk nauwelijks intercontinentaal (vooral Europese
# low-cost-netwerken); vanaf deze vertrekpunten scannen we daarom alleen Europese
# bestemmingen -- scheelt scanbudget op combinaties die toch zelden een deal opleveren.
EUROPE_ONLY_ORIGINS = {"RTM", "EIN"}

# Bestemmingen staan in destinations.csv; per tier (populariteit) staat in lib/planner.py
# hoeveel datumparen en hoe vaak er gescand wordt.

# Maximaal aantal COMBINATIES per run (elk kost 2-3 paginaladingen: 3 bij een geslaagd vertrek- EN
# retourraster, 1 bij een dubbele terugval). Was 1000, toen 500, maar een echte nachtelijke run op
# DEZE repo (na de toevoeging van het retourraster) crashte na 3u35m op combinatie 241/500
# (~54s/combinatie gemeten) -- vermoedelijk geheugenopbouw in een urenlange, ononderbroken
# browsersessie (zie BROWSER_RESTART_EVERY hieronder, die dat risico nu beperkt). Voorlopig naar 300
# om ruim binnen deze workflow's timeout-minutes (350, zie scan.yml) te passen, ook als een deel van
# de combinaties traag uitpakt; bijstellen zodra een paar runs met de periodieke browserherstart een
# stabieler beeld geven.
# Gemeten op echte GitHub Actions-runs: budget 300 = 2u58m (~36s/combinatie), maar budget 450 werd
# na 5u50m door scan.yml's timeout afgebroken op combinatie 271/450 (~77s/combinatie): het tempo
# wisselt sterk per nacht, dus 300 is de enige waarde die bewezen binnen de timeout (350 min) past.
SCANS_PER_RUN = 3

# Na zoveel combinaties wordt de browsersessie preventief afgesloten en vers geopend (zelfde
# consent-aanpak, accept_consent_if_present is daar al idempotent in). Een losse combinatie die
# alsnog fataal misgaat (bv. een kapotte pipe naar Playwright's Node-driver, zoals hierboven) krijgt
# dezelfde behandeling als een gewone mislukte scan (insight_label 'fout'), waarna de browser ook
# meteen herstart i.p.v. de rest van de run mee te slepen in een kapotte sessie.
BROWSER_RESTART_EVERY = 100

# Hoeveel maanden vooruit, en hoeveel steekproefdata per maand. Was 6; opgehoogd naar 8 zodat
# 6_calendar.py's 'goedkope maanden' een volledig jaarrond-beeld eerder compleet krijgt --
# veilig te verhogen zonder het nachtelijke budget aan te raken: dit vergroot alleen de pool aan
# (route, datum)-combinaties die na verloop van tijd aan de beurt komen (select_due_combos kiest
# nog steeds tot --budget per run), niet het aantal scans per nacht zelf.
MONTHS_AHEAD = 8
SEARCH_START_OFFSET_DAYS = 10  # nooit dichter bij vandaag zoeken dan dit

# Het vertrekraster wordt per (route, cabin class) doorgeklikt over MONTHS_AHEAD maanden (zie
# lib/scraper.fetch_calendar_prices(months=...)). Een andere maand dan die van de geplande
# combinatie waarvan de goedkoopste dag minstens OPVALLEND_KORTING onder de rastermediaan ligt
# (zelfde 20% als 2_curate.py's MIN_DISCOUNT_PCT) krijgt meteen een volledige scan, ook als die
# maand zelf nog niet aan de beurt is -- zo wacht een deal in maand 4 niet weken op zijn beurt.
# Gemaximeerd per route, want elke zo'n scan is een extra paginalading.
OPVALLEND_KORTING = 0.20
MAX_OPVALLENDE_MAANDEN_PER_ROUTE = 3

# Hoe ver we terugkijken naar eerdere scans (>= het langste tier-interval).
LOOKBACK_DAYS = max(t["interval_days"] for t in TIER_CONFIG.values())


# ============================================================
# DATUM-STEEKPROEF
# ============================================================

def generate_sample_dates(
    stay_days=STAY_DAYS,
    months_ahead=MONTHS_AHEAD,
    samples_per_month=1,
    start_offset_days=SEARCH_START_OFFSET_DAYS,
):
    """
    Genereert een lichte steekproef van (vertrek, terugkomst)-datumparen:
    een handvol representatieve data per maand, verspreid over de komende
    'months_ahead' maanden. Deze datums zijn het startpunt voor het kalenderraster
    (zie fetch_calendar_prices) -- niet per se de datum die uiteindelijk gescand wordt.
    """
    vandaag = datetime.now()
    vroegste = vandaag + timedelta(days=start_offset_days)

    # Verspreid de steekproefdagen evenwichtig over de maand (1-28), in plaats
    # van een vaste vroege lijst -- zo blijft er ook voor de eerstkomende
    # (deels al verstreken) maand een kans op geldige datums over.
    dagen_in_maand = [
        max(1, min(28, round((i + 1) * 28 / (samples_per_month + 1))))
        for i in range(samples_per_month)
    ]

    parenlijst = []
    for maand_offset in range(months_ahead):
        basis_maand = vroegste.month - 1 + maand_offset
        jaar = vroegste.year + basis_maand // 12
        maand = basis_maand % 12 + 1

        for dag in dagen_in_maand:
            try:
                depart = datetime(jaar, maand, dag)
            except ValueError:
                continue  # bijv. 30 februari bestaat niet, sla over
            if depart < vroegste:
                continue
            ret = depart + timedelta(days=stay_days)
            parenlijst.append((depart.strftime("%Y-%m-%d"), ret.strftime("%Y-%m-%d")))

    return parenlijst


# ============================================================
# SUPABASE
# ============================================================

def ensure_routes(client, pairs):
    """Zorgt dat alle (origin, destination)-routes bestaan; geeft {(o, d): route_id}."""
    pairs = sorted(set(pairs))
    route_ids = {}
    for i in range(0, len(pairs), 500):
        rows = [{"origin": o, "destination": d} for o, d in pairs[i:i + 500]]
        resp = client.table("routes").upsert(rows, on_conflict="origin,destination").execute()
        route_ids.update({(r["origin"], r["destination"]): r["id"] for r in resp.data})
    return route_ids


def load_last_scanned(client, route_ids):
    """
    {(origin, destination, depart_date, return_date, cabin_class): tijdstip van de
    laatste scan} over de laatste LOOKBACK_DAYS dagen (cabin_class erbij: Economy- en
    Business-scanrecentheid worden onafhankelijk bijgehouden, zie Combo.key). Mislukte
    scans tellen mee, zodat een geblokkeerde run bij een herstart niet meteen op
    dezelfde combinaties inslaat. Dankzij het kalenderraster (zie fetch_calendar_prices)
    bevat `scans` nu ook kale, snel opgebouwde dagprijzen rond elke gescande combinatie --
    die tellen hier vanzelf mee, dus een dag die al via een ANDERE combinatie se
    kalenderraster is meegenomen, geldt al als 'recent gescand'.

    Geen `.in_(route_id, ...)`-filter: met ~1.200 routes wordt de URL te lang.
    De tabel bevat alleen onze eigen routes; onbekende route_ids slaan we over.
    """
    route_per_id = {rid: od for od, rid in route_ids.items()}
    sinds = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    laatste = {}
    start, grootte = 0, 1000
    while True:
        resp = (
            client.table("scans")
            .select("route_id, depart_date, return_date, cabin_class, scanned_at")
            .gte("scanned_at", sinds)
            .order("id")
            .range(start, start + grootte - 1)
            .execute()
        )
        for r in resp.data:
            od = route_per_id.get(r["route_id"])
            if od is None:
                continue
            sleutel = (*od, r["depart_date"], r["return_date"], r["cabin_class"])
            tijdstip = datetime.fromisoformat(r["scanned_at"])
            if sleutel not in laatste or tijdstip > laatste[sleutel]:
                laatste[sleutel] = tijdstip
        if len(resp.data) < grootte:
            return laatste
        start += grootte



# ============================================================
# HOOFDPROGRAMMA
# ============================================================

def build_combos(origins, destinations):
    """
    Alle (vertrekpunt, bestemming, datumpaar)-combinaties, met tier-specifieke
    datumparen. Voor intercontinentale bestemmingen (land niet in lib.regions.EUROPE)
    komen er, met dezelfde datums/cadans, ook Business Class-combinaties bij -- behalve
    vanaf een vertrekpunt in EUROPE_ONLY_ORIGINS, dat slaat intercontinentale bestemmingen
    helemaal over (zie de toelichting bij EUROPE_ONLY_ORIGINS hierboven).
    """
    datums_per_sample = {}
    combos = []
    for dest in destinations:
        dest_is_europe = is_europe(dest["land"])
        min_stay, max_stay, retour_anker = stay_bounds(dest["tags"], dest_is_europe)
        n = TIER_CONFIG[dest["tier"]]["samples_per_month"]
        if n not in datums_per_sample:
            datums_per_sample[n] = generate_sample_dates(samples_per_month=n)
        cabin_classes = ["economy", "business"] if not dest_is_europe else ["economy"]
        for origin in origins:
            if origin == dest["iata"]:
                continue
            if origin in EUROPE_ONLY_ORIGINS and not dest_is_europe:
                continue
            for depart, ret in datums_per_sample[n]:
                for cabin_class in cabin_classes:
                    combos.append(Combo(
                        origin, dest["iata"], depart, ret, dest["tier"], cabin_class,
                        min_stay_days=min_stay, max_stay_days=max_stay, retour_anker_days=retour_anker,
                    ))
    return combos


def load_subscriber_wensen(client):
    """
    Haalt actuele abonnee-voorkeuren op uit `subscribers` (toegestaan voor de service-role-key,
    zie de private repo's migrations/008_subscribers.sql's RLS-policy: alleen INSERT voor anon,
    select blijft dus voorbehouden aan scripts die de service-role-key gebruiken, zoals dit
    script). [] bij een dry-run (geen client) -- dan is er sowieso geen scanprioriteit te bepalen
    op basis van iets wat we niet hebben opgehaald.
    """
    if client is None:
        return []
    return fetch_all(lambda: client.table("subscribers").select(
        "origin_airport, origin_airports, region, countries, period_from, period_to"
    ))


def maak_subscriber_prioriteit_fn(subscribers, land_per_iata):
    """
    Geeft een prioriteit_fn voor select_due_combos (zie lib/planner.py): 0 (dus eerst) voor een
    combinatie die bij MINSTENS ÉÉN abonnee-voorkeur past (vertrekpunt, bestemmingsland/regio,
    reisperiode -- dezelfde matchlogica als send_newsletter.py gebruikt om deals te kiezen, nu
    vooraf toegepast op WAT we scannen i.p.v. achteraf op wat we al gevonden hebben), anders 1.
    We weten dat daar aantoonbare interesse in is; de rest komt na die groep aan de beurt.
    Bewust geen gewicht naar AANTAL matchende abonnees -- dat zou populaire voorkeuren nog verder
    bevoordelen t.o.v. een enkele abonnee met een nichewens, wat niet de bedoeling is.
    """
    def matcht(combo, subscriber):
        if not matches_origin(subscriber, combo.origin):
            return False
        land = land_per_iata.get(combo.destination)
        if land is None:
            return False
        if not matches_countries(subscriber, land, is_europe(land)):
            return False
        return matches_period(subscriber, combo.depart_date)

    def prioriteit(combo):
        return 0 if any(matcht(combo, s) for s in subscribers) else 1

    return prioriteit


def nieuwe_browser_en_pagina(playwright):
    """Opent een verse browser + pagina (zelfde instellingen als main()'s oorspronkelijke, eenmalige
    sessie) -- gebruikt zowel bij de start van een run als bij een preventieve/noodgedwongen herstart
    (zie BROWSER_RESTART_EVERY)."""
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(locale="nl-NL", viewport=CALENDAR_VIEWPORT)
    return browser, page


def _vanaf_min_datum(dagen):
    """Alleen dagen die de scanner mag zoeken (niet dichter bij vandaag dan SEARCH_START_OFFSET_DAYS);
    het raster begint zelf al op vandaag."""
    min_datum = (datetime.now() + timedelta(days=SEARCH_START_OFFSET_DAYS)).strftime("%Y-%m-%d")
    return min_datum, [d for d in dagen if d["depart_date"] >= min_datum] or dagen


def scan_combo(page, client, route_id, combo, raster_al_opgehaald, raster_cache=None, gescand=None):
    """
    Handelt 1 geplande combinatie af: eerst een kalenderraster-poging (tenzij dit (route,
    cabin class) al in `raster_al_opgehaald` zit, zie de docstring bovenaan dit bestand) om de
    goedkoopste VERTREKdag van de maand te vinden, dan -- met dat vertrekpunt vast -- een tweede
    raster-poging om de goedkoopste RETOURdag te vinden (i.p.v. klakkeloos STAY_DAYS aan te houden),
    en daarna pas de volledige scan op die definitieve (vertrek, retour)-combinatie (of, bij een
    terugval op beide rasters, gewoon de geplande dag + vaste verblijfsduur).

    Het vertrekraster loopt over MONTHS_AHEAD maanden (doorgeklikt, zie fetch_calendar_prices) en
    wordt in `raster_cache` bewaard: een latere combinatie van dezelfde (route, cabin class) in deze
    run haalt niets opnieuw op maar kiest de goedkoopste dag van ZIJN maand uit die cache. Maanden
    die in het raster opvallend goedkoop zijn (zie OPVALLEND_KORTING) krijgen bij de eerste combinatie
    van de route meteen een volledige scan; `gescand` ({(origin, destination, cabin, vertrekdag)})
    onthoudt wat al volledig gescand is, zodat zo'n maand niet nog eens gescand wordt wanneer zijn
    eigen combinatie later aan de beurt komt (row is dan None).

    Geeft (row, bulk_rijen, extra_rijen) terug -- bulk_rijen zijn de kale rasterrijen (leeg bij
    een terugval of een al opgehaald raster), extra_rijen de volledig gescande opvallende maanden.
    """
    raster_cache = {} if raster_cache is None else raster_cache
    gescand = set() if gescand is None else gescand
    stay_days = (datetime.strptime(combo.return_date, "%Y-%m-%d")
                 - datetime.strptime(combo.depart_date, "%Y-%m-%d")).days

    werkelijke_depart, werkelijke_return = combo.depart_date, combo.return_date
    bulk_rijen, extra_rijen = [], []

    raster_sleutel = (combo.origin, combo.destination, combo.cabin_class)
    if raster_sleutel not in raster_al_opgehaald:
        raster_al_opgehaald.add(raster_sleutel)
        # Het raster start bij de maand van de opgegeven datum (en klikt alleen VOORUIT door): met de
        # geplande datum als startpunt zou een combinatie in bv. mei alleen mei-aug dekken. Daarom
        # altijd starten bij de vroegste zoekdatum, zodat het raster de hele periode dekt.
        raster_start = (datetime.now() + timedelta(days=SEARCH_START_OFFSET_DAYS)).strftime("%Y-%m-%d")
        dagen = fetch_calendar_prices(
            page, combo.origin, combo.destination, raster_start, stay_days,
            cabin_class=combo.cabin_class, months=MONTHS_AHEAD,
        )
        time.sleep(random.uniform(SEARCH_DELAY_MIN_SECONDS, SEARCH_DELAY_MAX_SECONDS))
        raster_cache[raster_sleutel] = dagen

        if dagen:
            min_datum, beschikbaar = _vanaf_min_datum(dagen)
            beste = select_cheapest_in_month(beschikbaar, combo.depart_date)
            werkelijke_depart, werkelijke_return = beste["depart_date"], beste["return_date"]
            print(f"    kalenderraster: {len(dagen)} dagen over {len({d['depart_date'][:7] for d in dagen})} "
                  f"maanden, goedkoopste in {combo.depart_date[:7]}: {werkelijke_depart} (€{beste['price']:.0f})")
            # De 'beste' dag krijgt zo dadelijk de volledige (label+heenreis) rij; geen
            # dubbele/verouderde kale rij voor diezelfde dag ernaast.
            bulk_rijen = [r for r in build_calendar_scan_rows(route_id, dagen, combo.cabin_class)
                          if r["depart_date"] != werkelijke_depart]

            retour_dagen = fetch_return_date_prices(
                page, combo.origin, combo.destination, werkelijke_depart,
                stay_days=combo.retour_anker_days, cabin_class=combo.cabin_class,
            )
            time.sleep(random.uniform(SEARCH_DELAY_MIN_SECONDS, SEARCH_DELAY_MAX_SECONDS))

            if retour_dagen:
                kandidaten = filter_by_stay_length(
                    retour_dagen, werkelijke_depart, combo.min_stay_days, combo.max_stay_days,
                )
                if kandidaten:
                    beste_retour = select_cheapest_return(kandidaten)
                    werkelijke_return = beste_retour["return_date"]
                    print(f"    retourraster: {len(retour_dagen)} opties ({len(kandidaten)} binnen "
                          f"{combo.min_stay_days}-{combo.max_stay_days} nachten), goedkoopste retour: "
                          f"{werkelijke_return} (€{beste_retour['price']:.0f})")
                else:
                    print(f"    retourraster leverde niets op binnen {combo.min_stay_days}-"
                          f"{combo.max_stay_days} nachten, behoud vaste verblijfsduur.")
                # Zelfde dedupe-redenering als bij het vertrekraster hierboven: de uiteindelijk
                # gekozen retourdag (indien van toepassing) krijgt zo dadelijk de volledige rij; de
                # rest is gewoon extra scangeschiedenis, ook buiten de min/max-grenzen.
                bulk_rijen += [r for r in build_calendar_scan_rows(route_id, retour_dagen, combo.cabin_class)
                               if r["return_date"] != werkelijke_return]
            else:
                print("    geen (bruikbaar) retourraster, behoud vaste verblijfsduur.")

            # Opvallend goedkope ANDERE maanden uit hetzelfde raster: nu al volledig scannen.
            for d in select_notable_months(
                dagen, combo.depart_date, min_datum, OPVALLEND_KORTING, MAX_OPVALLENDE_MAANDEN_PER_ROUTE,
            ):
                print(f"    opvallende maand {d['depart_date'][:7]}: {d['depart_date']} (€{d['price']:.0f}), "
                      f"volledige scan.")
                extra_insights = search_route_insights(
                    page, combo.origin, combo.destination, d["depart_date"], d["return_date"],
                    seat=combo.cabin_class,
                )
                extra_rijen.append(build_scan_row(
                    route_id, d["depart_date"], d["return_date"], extra_insights, cabin_class=combo.cabin_class,
                ))
                gescand.add((*raster_sleutel, d["depart_date"]))
                bulk_rijen = [r for r in bulk_rijen if r["depart_date"] != d["depart_date"]]
                time.sleep(random.uniform(SEARCH_DELAY_MIN_SECONDS, SEARCH_DELAY_MAX_SECONDS))
        else:
            print("    geen (bruikbaar) kalenderraster, val terug op de geplande datum.")
    elif raster_cache.get(raster_sleutel):
        # Raster van deze route zit al in de cache: kies de goedkoopste dag van de maand van DEZE
        # combinatie daaruit (zonder nieuw verzoek). Zit die maand er niet in, dan blijft de
        # geplande datum staan.
        _, beschikbaar = _vanaf_min_datum(raster_cache[raster_sleutel])
        in_maand = [d for d in beschikbaar if d["depart_date"][:7] == combo.depart_date[:7]]
        if in_maand:
            beste = min(in_maand, key=lambda d: (d["price"], d["depart_date"]))
            if (*raster_sleutel, beste["depart_date"]) in gescand:
                print(f"    {beste['depart_date']} is al eerder deze run volledig gescand, overgeslagen.")
                return None, [], []
            werkelijke_depart, werkelijke_return = beste["depart_date"], beste["return_date"]

    gescand.add((*raster_sleutel, werkelijke_depart))
    insights = search_route_insights(
        page, combo.origin, combo.destination, werkelijke_depart, werkelijke_return, seat=combo.cabin_class,
    )
    row = build_scan_row(route_id, werkelijke_depart, werkelijke_return, insights, cabin_class=combo.cabin_class)
    return row, bulk_rijen, extra_rijen


def main():
    parser = argparse.ArgumentParser(description="Scan Google Flights Prijsinzichten -> Supabase")
    parser.add_argument("--budget", type=int, default=SCANS_PER_RUN,
                        help=f"maximaal aantal combinaties deze run (standaard {SCANS_PER_RUN})")
    parser.add_argument("--limit", type=int, help="stop na dit aantal combinaties (om te testen)")
    parser.add_argument("--max-minuten", type=float, default=None,
                        help="stop netjes (na de lopende combinatie) zodra de run zo lang bezig is; "
                             "bedoeld voor GitHub Actions, zie scan.yml")
    parser.add_argument("--dest", help="alleen deze bestemmingen, kommagescheiden IATA-codes")
    parser.add_argument("--dry-run", action="store_true", help="niets naar Supabase schrijven of ervan lezen")
    args = parser.parse_args()

    bestemmingen = load_destinations()
    if args.dest:
        gevraagd = {c.strip().upper() for c in args.dest.split(",") if c.strip()}
        onbekend = gevraagd - {b["iata"] for b in bestemmingen}
        if onbekend:
            parser.error(f"onbekende bestemming(en) in destinations.csv: {', '.join(sorted(onbekend))}")
        bestemmingen = [b for b in bestemmingen if b["iata"] in gevraagd]

    combos = build_combos(ORIGIN_AIRPORTS, bestemmingen)
    per_tier = {t: sum(1 for b in bestemmingen if b["tier"] == t) for t in TIER_CONFIG}
    n_business = sum(1 for c in combos if c.cabin_class == "business")
    print(f"{len(bestemmingen)} bestemmingen (tier 1-4: "
          f"{', '.join(str(per_tier[t]) for t in sorted(TIER_CONFIG))}) x {len(ORIGIN_AIRPORTS)} vertrekpunten "
          f"= {len(combos)} (route, datum, cabin)-combinaties "
          f"({len(combos) - n_business} economy, {n_business} business).")
    print(f"Doelbelasting bij volledig rooster: ~{steady_state_load(combos):.0f} scans/dag; "
          f"budget deze run: {args.budget}.\n")

    if args.dry_run:
        client, route_ids, last_scanned = None, {}, {}
        print("DRY-RUN: er wordt niets naar Supabase geschreven.\n")
    else:
        from lib.db import get_client  # pas hier: een dry-run heeft geen .env nodig
        client = get_client()
        route_ids = ensure_routes(client, {(c.origin, c.destination) for c in combos})
        last_scanned = load_last_scanned(client, route_ids)

    subscribers = load_subscriber_wensen(client)
    land_per_iata = {b["iata"]: b["land"] for b in bestemmingen}
    prioriteit_fn = maak_subscriber_prioriteit_fn(subscribers, land_per_iata)

    budget = min(args.budget, args.limit) if args.limit is not None else args.budget
    te_scannen, n_aan_de_beurt = select_due_combos(
        combos, last_scanned, datetime.now(timezone.utc), budget, prioriteit_fn=prioriteit_fn,
    )
    n_subscriber_relevant = sum(1 for c in te_scannen if prioriteit_fn(c) == 0)
    print(f"{n_aan_de_beurt} combinaties zijn aan de beurt; {len(te_scannen)} worden deze run gescand "
          f"({n_subscriber_relevant} daarvan sluiten aan bij een abonnee-voorkeur, {len(subscribers)} "
          f"abonnees).\n")

    n_nieuw = n_deals = n_fouten = n_db_fouten = 0
    opeenvolgende_fouten = 0
    raster_al_opgehaald = set()
    raster_cache, gescand = {}, set()
    start = time.monotonic()
    verbruikt = 0  # extra volledige scans (opvallende maanden) tellen mee voor het budget

    with sync_playwright() as p:
        # Eén browsersessie voor zo'n honderdtal combinaties (BROWSER_RESTART_EVERY): de
        # consent-cookie blijft behouden binnen die sessie. Vast venstergrootte nodig voor het
        # kalenderraster (zie lib/scraper.CALENDAR_VIEWPORT).
        browser, page = nieuwe_browser_en_pagina(p)

        for i, combo in enumerate(te_scannen):
            origin, destination = combo.origin, combo.destination
            route_id = route_ids.get((origin, destination))

            print(f"[{n_nieuw + 1}/{len(te_scannen)}] {origin} -> {destination} "
                  f"(tier {combo.tier}, {combo.cabin_class}), gepland heen {combo.depart_date}, "
                  f"terug {combo.return_date}")
            try:
                row, bulk_rijen, extra_rijen = scan_combo(
                    page, client, route_id, combo, raster_al_opgehaald, raster_cache, gescand,
                )
            except Exception as e:
                # Een fatale fout in de browsersessie zelf (bv. een kapotte pipe naar Playwright's
                # Node-driver na urenlang draaien) -- de bestaande retries in lib/scraper.py vangen
                # dit soort crashes niet op, want de hele sessie is dan onbruikbaar. Behandel deze
                # combinatie als een gewone mislukte scan en herstart de sessie voor de rest van de run.
                print(f"  FATALE FOUT in de browsersessie ({type(e).__name__}: {e}), "
                      f"browsersessie wordt herstart.\n")
                row = build_scan_row(route_id, combo.depart_date, combo.return_date, None, cabin_class=combo.cabin_class)
                bulk_rijen, extra_rijen = [], []
                try:
                    browser.close()
                except Exception:
                    pass
                browser, page = nieuwe_browser_en_pagina(p)

            if bulk_rijen and client is not None and not save_scans_bulk(client, bulk_rijen):
                n_db_fouten += 1

            for extra in extra_rijen:
                if extra["is_deal"]:
                    n_deals += 1
                    print(f"  Opvallende maand {extra['depart_date'][:7]}: €{extra['lowest_price']:.0f}, "
                          f"label: {extra['insight_label']} <<< DEAL")
                if client is not None and not save_scan(client, extra):
                    n_db_fouten += 1
                    print("  WAARSCHUWING: deze extra scan is NIET opgeslagen.\n")

            if row is None:
                # Maand al volledig gescand als opvallende maand eerder deze run: niets te doen.
                n_nieuw += 1
                print()
                continue

            if row["insight_label"] == "fout":
                print("  Geen resultaat (fout na retries).\n")
                n_fouten += 1
                opeenvolgende_fouten += 1
            else:
                opeenvolgende_fouten = 0
                if row["lowest_price"] is None:
                    print("  Geen prijs gevonden op de pagina (waarschijnlijk geen vluchten "
                          "beschikbaar voor deze combinatie).\n")
                else:
                    heen = ""
                    if row["outbound_stops"] is not None:
                        maatschappij = ", ".join(row["outbound_airlines"]) if row["outbound_airlines"] else "onbekend"
                        heen = (f", heenreis: {row['outbound_stops']}x overstappen"
                                f"{' via ' + ', '.join(row['outbound_stopover_airports']) if row['outbound_stops'] else ''}"
                                f", {row['outbound_duration_minutes']} min, {maatschappij}")
                    print(f"  Prijs: €{row['lowest_price']:.0f}, label: {row['insight_label']}{heen}"
                          f"{' <<< DEAL' if row['is_deal'] else ''}\n")
                if row["is_deal"]:
                    n_deals += 1

            if client is not None and not save_scan(client, row):
                n_db_fouten += 1
                print("  WAARSCHUWING: deze scan is NIET opgeslagen.\n")

            n_nieuw += 1
            verbruikt += len(extra_rijen)

            if opeenvolgende_fouten >= MAX_CONSECUTIVE_ERRORS:
                print(f"\nSTOP: {MAX_CONSECUTIVE_ERRORS} mislukte zoekopdrachten achter elkaar -- "
                      f"waarschijnlijk geblokkeerd. Wacht een tijd en probeer het later opnieuw.")
                break

            time.sleep(random.uniform(SEARCH_DELAY_MIN_SECONDS, SEARCH_DELAY_MAX_SECONDS))

            if n_nieuw + verbruikt >= budget:
                print(f"\nBudget ({budget}) bereikt, inclusief {verbruikt} extra scans voor opvallende maanden.")
                break

            if args.max_minuten is not None and (time.monotonic() - start) / 60 >= args.max_minuten:
                print(f"\nTijdslimiet ({args.max_minuten:.0f} min) bereikt na {n_nieuw + verbruikt} scans; "
                      f"de rest is de volgende ronde/nacht aan de beurt.")
                break

            if (i + 1) % BROWSER_RESTART_EVERY == 0 and (i + 1) < len(te_scannen):
                print(f"  (preventieve herstart van de browsersessie na {BROWSER_RESTART_EVERY} combinaties)\n")
                browser.close()
                browser, page = nieuwe_browser_en_pagina(p)

        browser.close()

    print(f"\n=== Klaar. {n_nieuw} combinaties gescand, {n_deals} als 'laag' gemarkeerd, "
          f"{n_fouten} mislukt. ===")
    if n_db_fouten:
        print(f"LET OP: {n_db_fouten} scans konden niet naar Supabase worden weggeschreven.")


if __name__ == "__main__":
    main()
