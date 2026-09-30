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

De losse dagprijzen uit het kalenderraster worden ook zelf weggeschreven (kaal: alleen
prijs, geen label/heenreis -- dat kent het raster niet) als extra, snellere scangeschiedenis
voor 2_curate.py's discount_pct-mediaan. Binnen één run wordt het kalenderraster van
eenzelfde (route, cabin class) maar 1x opgehaald (RASTER_AL_OPGEHAALD hieronder) -- bij
tier 1 (2 steekproefdata/maand) zou een tweede keer grotendeels dezelfde ~60 dagen
opnieuw ophalen, puur dubbel werk.

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

from lib.destinations import load_destinations
from lib.planner import TIER_CONFIG, Combo, select_due_combos, steady_state_load
from lib.regions import is_europe
from lib.scraper import (
    CALENDAR_VIEWPORT,
    MAX_CONSECUTIVE_ERRORS,
    SEARCH_DELAY_MAX_SECONDS,
    SEARCH_DELAY_MIN_SECONDS,
    STAY_DAYS,
    build_calendar_scan_rows,
    build_scan_row,
    fetch_calendar_prices,
    save_scan,
    save_scans_bulk,
    search_route_insights,
    select_cheapest_in_month,
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

# Bestemmingen staan in destinations.csv; per tier (populariteit) staat in lib/planner.py
# hoeveel datumparen en hoe vaak er gescand wordt.

# Maximaal aantal COMBINATIES per run (elk kost 1-2 paginaladingen: 2 bij een geslaagd
# kalenderraster, 1 bij een terugval). Was 1000, maar op de private repo gemeten duurtijd
# per combinatie bleek sterk te wisselen (twee losse metingen op dezelfde dag: ~37s en
# ~217s/combinatie) -- vermoedelijk door de opgetelde testbelasting van die dag, niet per
# se representatief voor een normale run. Voorlopig bewust lager gezet zodat de run
# betrouwbaar op tijd klaar is voor de rest van de pijplijn (zie CLAUDE.md in de private
# repo over de repository_dispatch-koppeling naar pipeline.yml daar); bijstellen zodra een
# paar echte runs (GitHub Actions' eigen logs) een stabieler beeld geven.
SCANS_PER_RUN = 500

# Hoeveel maanden vooruit, en hoeveel steekproefdata per maand. Was 6; opgehoogd naar 8 zodat
# 6_calendar.py's 'goedkope maanden' een volledig jaarrond-beeld eerder compleet krijgt --
# veilig te verhogen zonder het nachtelijke budget aan te raken: dit vergroot alleen de pool aan
# (route, datum)-combinaties die na verloop van tijd aan de beurt komen (select_due_combos kiest
# nog steeds tot --budget per run), niet het aantal scans per nacht zelf.
MONTHS_AHEAD = 8
SEARCH_START_OFFSET_DAYS = 10  # nooit dichter bij vandaag zoeken dan dit

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
    komen er, met dezelfde datums/cadans, ook Business Class-combinaties bij.
    """
    datums_per_sample = {}
    combos = []
    for dest in destinations:
        n = TIER_CONFIG[dest["tier"]]["samples_per_month"]
        if n not in datums_per_sample:
            datums_per_sample[n] = generate_sample_dates(samples_per_month=n)
        cabin_classes = ["economy", "business"] if not is_europe(dest["land"]) else ["economy"]
        for origin in origins:
            if origin == dest["iata"]:
                continue
            for depart, ret in datums_per_sample[n]:
                for cabin_class in cabin_classes:
                    combos.append(Combo(origin, dest["iata"], depart, ret, dest["tier"], cabin_class))
    return combos


def scan_combo(page, client, route_id, combo, raster_al_opgehaald):
    """
    Handelt 1 geplande combinatie af: eerst een kalenderraster-poging (tenzij dit (route,
    cabin class) al in `raster_al_opgehaald` zit, zie de docstring bovenaan dit bestand),
    dan de volledige scan op de daadwerkelijk goedkoopste dag van die maand (of, bij een
    terugval, gewoon de geplande dag). Geeft (row, bulk_rijen) terug -- bulk_rijen is een
    lege lijst bij een terugval of een al opgehaald raster.
    """
    stay_days = (datetime.strptime(combo.return_date, "%Y-%m-%d")
                 - datetime.strptime(combo.depart_date, "%Y-%m-%d")).days

    werkelijke_depart, werkelijke_return = combo.depart_date, combo.return_date
    bulk_rijen = []

    raster_sleutel = (combo.origin, combo.destination, combo.cabin_class)
    if raster_sleutel not in raster_al_opgehaald:
        raster_al_opgehaald.add(raster_sleutel)
        dagen = fetch_calendar_prices(
            page, combo.origin, combo.destination, combo.depart_date, stay_days,
            cabin_class=combo.cabin_class,
        )
        time.sleep(random.uniform(SEARCH_DELAY_MIN_SECONDS, SEARCH_DELAY_MAX_SECONDS))

        if dagen:
            beste = select_cheapest_in_month(dagen, combo.depart_date)
            werkelijke_depart, werkelijke_return = beste["depart_date"], beste["return_date"]
            print(f"    kalenderraster: {len(dagen)} dagen, goedkoopste in {combo.depart_date[:7]}: "
                  f"{werkelijke_depart} (€{beste['price']:.0f})")
            # De 'beste' dag krijgt zo dadelijk de volledige (label+heenreis) rij; geen
            # dubbele/verouderde kale rij voor diezelfde dag ernaast.
            bulk_rijen = [r for r in build_calendar_scan_rows(route_id, dagen, combo.cabin_class)
                          if r["depart_date"] != werkelijke_depart]
        else:
            print("    geen (bruikbaar) kalenderraster, val terug op de geplande datum.")

    insights = search_route_insights(
        page, combo.origin, combo.destination, werkelijke_depart, werkelijke_return, seat=combo.cabin_class,
    )
    row = build_scan_row(route_id, werkelijke_depart, werkelijke_return, insights, cabin_class=combo.cabin_class)
    return row, bulk_rijen


def main():
    parser = argparse.ArgumentParser(description="Scan Google Flights Prijsinzichten -> Supabase")
    parser.add_argument("--budget", type=int, default=SCANS_PER_RUN,
                        help=f"maximaal aantal combinaties deze run (standaard {SCANS_PER_RUN})")
    parser.add_argument("--limit", type=int, help="stop na dit aantal combinaties (om te testen)")
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

    budget = min(args.budget, args.limit) if args.limit is not None else args.budget
    te_scannen, n_aan_de_beurt = select_due_combos(combos, last_scanned, datetime.now(timezone.utc), budget)
    print(f"{n_aan_de_beurt} combinaties zijn aan de beurt; {len(te_scannen)} worden deze run gescand.\n")

    n_nieuw = n_deals = n_fouten = n_db_fouten = 0
    opeenvolgende_fouten = 0
    raster_al_opgehaald = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # 1 browsersessie voor de hele run: de consent-cookie blijft behouden. Vast
        # venstergrootte nodig voor het kalenderraster (zie lib/scraper.CALENDAR_VIEWPORT).
        page = browser.new_page(locale="nl-NL", viewport=CALENDAR_VIEWPORT)

        for combo in te_scannen:
            origin, destination = combo.origin, combo.destination
            route_id = route_ids.get((origin, destination))

            print(f"[{n_nieuw + 1}/{len(te_scannen)}] {origin} -> {destination} "
                  f"(tier {combo.tier}, {combo.cabin_class}), gepland heen {combo.depart_date}, "
                  f"terug {combo.return_date}")
            row, bulk_rijen = scan_combo(page, client, route_id, combo, raster_al_opgehaald)

            if bulk_rijen and client is not None and not save_scans_bulk(client, bulk_rijen):
                n_db_fouten += 1

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

            if opeenvolgende_fouten >= MAX_CONSECUTIVE_ERRORS:
                print(f"\nSTOP: {MAX_CONSECUTIVE_ERRORS} mislukte zoekopdrachten achter elkaar -- "
                      f"waarschijnlijk geblokkeerd. Wacht een tijd en probeer het later opnieuw.")
                break

            time.sleep(random.uniform(SEARCH_DELAY_MIN_SECONDS, SEARCH_DELAY_MAX_SECONDS))

        browser.close()

    print(f"\n=== Klaar. {n_nieuw} combinaties gescand, {n_deals} als 'laag' gemarkeerd, "
          f"{n_fouten} mislukt. ===")
    if n_db_fouten:
        print(f"LET OP: {n_db_fouten} scans konden niet naar Supabase worden weggeschreven.")


if __name__ == "__main__":
    main()
