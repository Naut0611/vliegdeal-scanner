"""
Scanner -- Google Flights "Prijsinzichten" -> tabel `scans` in Supabase
========================================================================

Dit is de public-repo kopie van de private repo's `analytics/1_scanner.py`
(zelfde logica, andere locatie): scraping-code staat bewust in een public
repo zodat de nachtelijke run gratis, onbeperkt op GitHub Actions kan draaien
(zie CLAUDE.md in de private repo voor de achtergrond van die split). De
private repo blijft leidend voor de architectuurdocumentatie; wijzigingen
hier horen ook daar (of andersom) doorgevoerd te worden.

Kernidee: in plaats van zelf, per route, een zware historische prijs-baseline
op te bouwen, lezen we Google Flights' EIGEN ingebouwde vergelijking uit: het
"Prijsinzichten"-blok. Dat blok vertelt of een prijs laag/gemiddeld/hoog is
t.o.v. wat Google als normaal beschouwt voor die route. Daardoor is er maar 1
paginalading per (route, datum)-combinatie nodig.

Elke combinatie levert 1 rij in de tabel `scans` op. Er zijn ~400 bestemmingen
(destinations.csv), veel te veel voor één run: lib/planner.py kiest
per run de meest achterstallige combinaties, tot `--budget` scans (populaire
bestemmingen vaker, obscure zeldzamer). Deze stap publiceert zelf NIETS: de
private repo's 2_curate.py, 3_recheck.py en de menselijke review (4_review.py)
bepalen wat er op de website komt.

Naast Economy scannen we ook Business Class, voor alle intercontinentale bestemmingen
(destinations.land niet in lib/regions.EUROPE) -- bewust, met dezelfde tier-cadans als
hun Economy-scan; dit verhoogt het scanvolume aanzienlijk (zie CLAUDE.md). Elke scan
legt ook (uit dezelfde paginalading) het aantal tussenstops, de tussenstop-
luchthaven(s) en de reisduur van de HEENreis vast (lib/scraper.extract_outbound_itinerary).

LET OP: dit scraped Google Flights (via een echte, headless browser), wat in
strijd is met hun gebruiksvoorwaarden. Gebruik met mate: spreid zoekopdrachten
over tijd, draai nooit meerdere scanners tegelijk, en behandel de instellingen
als een bewuste afweging tussen dekking en risico op blokkade (vertraging en
retries staan in lib/scraper.py).

Gebruik (vanuit de projectroot van DEZE repo):
    python scanner.py                 # normale run (SCANS_PER_RUN scans), schrijft naar Supabase
    python scanner.py --budget 50     # kleinere run
    python scanner.py --limit 3       # stop na 3 scans (om te testen)
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
    MAX_CONSECUTIVE_ERRORS,
    SEARCH_DELAY_MAX_SECONDS,
    SEARCH_DELAY_MIN_SECONDS,
    STAY_DAYS,
    build_scan_row,
    save_scan,
    search_route_insights,
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
    # Later uit te breiden met o.a.: RTM, EIN, CRL, CGN, FRA, MUC, HAM, ...
]

# Bestemmingen staan in analytics/destinations.csv; per tier (populariteit) staat
# in analytics/lib/planner.py hoeveel datumparen en hoe vaak er gescand wordt.

# Maximaal aantal scans per run. ~15-17 s per scan, dus 500 scans ≈ 2,5 uur.
# Hoger = snellere dekking, maar meer verkeer richting Google (blokkaderisico).
SCANS_PER_RUN = 10

# Hoeveel maanden vooruit, en hoeveel steekproefdata per maand.
MONTHS_AHEAD = 6
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
    'months_ahead' maanden.
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
    dezelfde combinaties inslaat.

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


def main():
    parser = argparse.ArgumentParser(description="Scan Google Flights Prijsinzichten -> Supabase")
    parser.add_argument("--budget", type=int, default=SCANS_PER_RUN,
                        help=f"maximaal aantal scans deze run (standaard {SCANS_PER_RUN})")
    parser.add_argument("--limit", type=int, help="stop na dit aantal scans (om te testen)")
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
    print(f"{n_aan_de_beurt} combinaties zijn aan de beurt; {len(te_scannen)} worden deze run gescand "
          f"(~{len(te_scannen) * 16 / 3600:.1f} uur).\n")

    n_nieuw = n_deals = n_fouten = n_db_fouten = 0
    opeenvolgende_fouten = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # 1 browsersessie voor de hele run: de consent-cookie blijft behouden.
        page = browser.new_page(locale="nl-NL")

        for combo in te_scannen:
            origin, destination = combo.origin, combo.destination
            depart_date, return_date = combo.depart_date, combo.return_date
            route_id = route_ids.get((origin, destination))

            print(f"[{n_nieuw + 1}/{len(te_scannen)}] {origin} -> {destination} "
                  f"(tier {combo.tier}, {combo.cabin_class}), heen {depart_date}, terug {return_date}")
            insights = search_route_insights(page, origin, destination, depart_date, return_date, seat=combo.cabin_class)
            row = build_scan_row(route_id, depart_date, return_date, insights, cabin_class=combo.cabin_class)

            if insights is None:
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
                        heen = (f", heenreis: {row['outbound_stops']}x overstappen"
                                f"{' via ' + ', '.join(row['outbound_stopover_airports']) if row['outbound_stops'] else ''}"
                                f", {row['outbound_duration_minutes']} min")
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
