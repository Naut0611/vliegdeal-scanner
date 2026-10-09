"""
Stap 6: calendar -- 'goedkope maanden' per route, uit de volledige scangeschiedenis
====================================================================================

Leest ALLE scans met een prijs (geen tijdvenster, in tegenstelling tot 2_curate.py's
HISTORY_DAYS: hoe langer de site draait, hoe betrouwbaarder het seizoensbeeld) en
berekent per (route, cabin class, kalendermaand) de mediaanprijs. Een maand telt als
'goedkoop' als die mediaan minstens CHEAP_THRESHOLD_PCT % onder de algehele mediaan
van die route/cabin class ligt -- vergelijkbaar met discount_pct in curated_deals,
maar dan per maand i.p.v. per datumpaar. Schrijft naar route_price_calendar (zie
migrations/007_route_price_calendar.sql), dat de website rechtstreeks leest (net als
curated_deals: publiek leesbaar, RLS staat alleen select toe).

Alleen (route, cabin_class, maand)-combinaties met minstens MIN_SAMPLES scans krijgen
een rij: zonder genoeg historie liever niets tonen dan een onbetrouwbare uitspraak
(dezelfde aanpak als deal_score's `None zonder historie`). Vroeg na de start van de
site zullen de meeste maanden dus nog ontbreken -- dat vult zich vanzelf aan naarmate
1_scanner.py langer draait (elke maand komt na verloop van tijd binnen bereik van
MONTHS_AHEAD, zie 1_scanner.py). Dit script publiceert niets rechtstreeks op de site
en heeft geen scraping-etiquette nodig (geen netwerkverkeer naar Google), dus geen
lock: veilig om los van de scan-run te draaien.

Gebruik (vanuit de projectroot):
    python 6_calendar.py               # berekenen en schrijven
    python 6_calendar.py --dry-run      # alleen tonen, niets schrijven
"""

import argparse
from statistics import median

from lib.db import fetch_all, get_client

MIN_SAMPLES = 4          # minstens zoveel scans in die maand voordat we 'm meetellen
CHEAP_THRESHOLD_PCT = 8  # maandmediaan moet minstens dit % onder de route-mediaan liggen


def month_of(depart_date: str) -> int:
    return int(depart_date[5:7])


def route_overall_median(history):
    """{(route_id, cabin_class): mediaan van lowest_price over de HELE geschiedenis}."""
    prijzen = {}
    for s in history:
        prijzen.setdefault((s["route_id"], s["cabin_class"]), []).append(float(s["lowest_price"]))
    return {sleutel: median(p) for sleutel, p in prijzen.items()}


def month_medians(history):
    """{(route_id, cabin_class, maand): (mediaan, aantal scans)}."""
    prijzen = {}
    for s in history:
        sleutel = (s["route_id"], s["cabin_class"], month_of(s["depart_date"]))
        prijzen.setdefault(sleutel, []).append(float(s["lowest_price"]))
    return {sleutel: (median(p), len(p)) for sleutel, p in prijzen.items()}


def compute_calendar(history, min_samples=MIN_SAMPLES, cheap_threshold_pct=CHEAP_THRESHOLD_PCT):
    """
    Geeft een rij per (route, cabin class, maand) met >= min_samples scans terug:
    {"route_id", "cabin_class", "month", "sample_count", "median_price", "cheap"}.
    """
    overall = route_overall_median(history)
    rijen = []
    for (route_id, cabin_class, maand), (mediaan, n) in month_medians(history).items():
        if n < min_samples:
            continue
        totaal = overall.get((route_id, cabin_class))
        cheap = totaal is not None and totaal > 0 and mediaan <= totaal * (1 - cheap_threshold_pct / 100)
        rijen.append({
            "route_id": route_id,
            "cabin_class": cabin_class,
            "month": maand,
            "sample_count": n,
            "median_price": round(mediaan, 2),
            "cheap": cheap,
        })
    return rijen


def main():
    parser = argparse.ArgumentParser(description="Scans -> route_price_calendar ('goedkope maanden' per route)")
    parser.add_argument("--dry-run", action="store_true", help="alleen tonen, niets schrijven")
    args = parser.parse_args()

    client = get_client()
    history = fetch_all(
        lambda: client.table("scans")
        .select("route_id, cabin_class, depart_date, lowest_price")
        .not_.is_("lowest_price", "null")
    )
    print(f"{len(history)} scans met prijs in de volledige geschiedenis.")

    routes = {r["id"]: r for r in fetch_all(lambda: client.table("routes").select("id, origin, destination"))}
    rijen = compute_calendar(history)
    for r in rijen:
        route = routes.get(r["route_id"])
        if route is None:
            continue  # verweesde route_id (zou niet moeten voorkomen): overslaan i.p.v. crashen
        r["origin"], r["destination"] = route["origin"], route["destination"]
    rijen = [r for r in rijen if "origin" in r]

    n_cheap = sum(1 for r in rijen if r["cheap"])
    print(f"{len(rijen)} (route, cabin class, maand)-combinaties met minstens {MIN_SAMPLES} scans, "
          f"waarvan {n_cheap} 'goedkoop' (>= {CHEAP_THRESHOLD_PCT}% onder de route-mediaan).")

    if args.dry_run:
        print("DRY-RUN: niets geschreven.")
        return

    if rijen:
        client.table("route_price_calendar").upsert(
            rijen, on_conflict="route_id,cabin_class,month"
        ).execute()
        print(f"{len(rijen)} rijen geschreven naar route_price_calendar.")
    else:
        print("Niets te schrijven.")


if __name__ == "__main__":
    main()
