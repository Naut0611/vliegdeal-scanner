"""
Stap 2: curate -- van ruwe scans naar kandidaat-deals
=====================================================

Leest de tabel `scans`, bepaalt per route de beste actuele deal en schrijft die
naar `curated_deals` met published = false. Er wordt NIETS gepubliceerd: een
mens moet de kandidaten eerst goedkeuren via `python 4_review.py`
(na de hercontrole in `3_recheck.py`).

Wat is een deal? Een datumpaar waarvan de MEEST RECENTE scan (van de laatste
`--days` dagen) door Google als 'laag' is bestempeld. Hoe goed die deal is,
drukken we uit in een score, op basis van onze eigen scanhistorie:

  discount_pct  % onder de mediaan van de recentste prijzen van ALLE datumparen
                van die route (min. 5 datumparen nodig). Let op: dit mengt
                seizoenen, dus een kerstvertrek scoort zelden hoog.
  drop_pct      % onder de mediaan van eerdere scans (> 1 dag oud) van
                HETZELFDE datumpaar, dus: is de prijs recent gedaald?
  deal_score    discount_pct + drop_pct (negatieve delen tellen als 0), plus
                FLOOR_BONUS als de prijs vlak boven Google's genoemde ondergrens
                ('onwaarschijnlijk dat de prijzen dalen tot onder € X') zit, plus
                RARITY_BONUS op basis van de tier van de bestemming (zie
                destinations.tier / destinations.csv, verder ook gebruikt om de
                scanfrequentie te bepalen, lib/planner.py): een bestemming die
                sowieso al vaak een deal heeft (tier 1) is minder bijzonder dan
                eenzelfde korting op een bestemming die zelden een deal heeft
                (tier 4) -- die laatste krijgt dus een hogere bonus.
                None als er nog geen historie is om mee te vergelijken.

Per (route, cabin class) wordt de deal met de hoogste score gekozen (gelijk: laagste
prijs) -- Economy en Business worden nooit samengevoegd: een route kan dus tegelijk
een Economy- én een Business-kandidaat hebben. `outbound_stops`,
`outbound_stopover_airports`, `outbound_duration_minutes` en `outbound_airlines`
(heenreis; zie lib/scraper.py) worden uit de gekozen scan overgenomen, puur
informatief -- ze doen niet mee in de score.

Kwaliteitsgrens: een scan telt alleen mee als kandidaat als `deal_score >= MIN_SCORE`
ÉN als de ECHTE korting (discount_pct of drop_pct, het hoogste van de twee --
dus los van FLOOR_BONUS/RARITY_BONUS) minstens `MIN_DISCOUNT_PCT` is. Die tweede
grens is bewust apart van deal_score: RARITY_BONUS (tot +15, puur omdat een
bestemming zelden een deal heeft) en FLOOR_BONUS (+10, prijs vlak bij Google's
ondergrens) kunnen een deal met een verwaarloosbare échte korting anders toch
over MIN_SCORE tillen -- een deal die "maar" 12% onder de route-mediaan zit is
geen waanzinnige deal, ongeacht hoe zeldzaam de bestemming is. Dat sluit ook
kandidaten ZONDER historie uit (discount_pct/drop_pct allebei None, dus 'geen
vergelijking mogelijk' telt niet als 'goed genoeg') -- bewust liever kwaliteit dan
kwantiteit: minder kandidaten in de review, maar allemaal met een aantoonbare, forse
korting.
Business Class boven `MAX_BUSINESS_PRICE` (nu: EUR 2000) komt sowieso niet in
aanmerking, ook niet met een hoge score -- boven dat bedrag is het geen goede deal.
Ook in totaal mag Business hooguit `MAX_BUSINESS_SHARE` (nu: 20%) van alle
kandidaten uitmaken; is er meer Business-materiaal dan dat, dan vallen de laagst
scorende Business-kandidaten af zodat de verhouding (minstens 80% Economy) klopt.

Regels voor bestaande rijen (zodat een eerdere beoordeling niet verloren gaat):
  - dezelfde deal, zelfde prijs  -> beoordeling ongemoeid laten; alleen de
                                    scorekolommen worden ververst
  - dezelfde deal, andere prijs  -> prijs bijwerken en terug naar 'wacht op review'
                                    (de mens keurde een ander bedrag goed)
  - onbeoordeelde rij die de hercontrole (3_recheck.py) 'verlopen' noemde,
    maar nu weer een deal is -> terug naar een schone 'wacht op review'

Gebruik (vanuit de projectroot):
    python 2_curate.py               # scans van de laatste 7 dagen
    python 2_curate.py --days 3      # kortere terugblik
    python 2_curate.py --dry-run     # alleen tonen, niets schrijven
"""

import argparse
from datetime import date, datetime, timedelta, timezone
from statistics import median

from lib.db import fetch_all, get_client

DEFAULT_DAYS = 7          # scans van de laatste N dagen komen in aanmerking als deal
HISTORY_DAYS = 60         # zoveel dagen scans gebruiken als vergelijkingsmateriaal
MIN_COMBOS_FOR_DISCOUNT = 5
MIN_AGE_FOR_DROP = timedelta(days=1)
FLOOR_TOLERANCE = 1.05    # 'vlak boven de ondergrens' = max. 5% erboven
FLOOR_BONUS = 10.0
# tier -> bonus (1 = bestemming heeft sowieso vaak een deal, 4 = zeldzaam/onbekend, zie destinations.tier)
RARITY_BONUS = {1: 0.0, 2: 5.0, 3: 10.0, 4: 15.0}
# Kwaliteitsgrens: kandidaten met een lagere (of geen) score worden niet gecurate.
MIN_SCORE = 10.0
# Harde ondergrens op de ECHTE korting (discount_pct of drop_pct, los van de bonussen
# hierboven): zonder deze grens kan een deal met een verwaarloosbare korting alsnog
# over MIN_SCORE komen puur op RARITY_BONUS/FLOOR_BONUS. Zie de toelichting bovenaan.
MIN_DISCOUNT_PCT = 30.0
# Reistijd-filter op de HEENreis (outbound_duration_minutes; onbekend = niet filteren):
# een deal valt af als de reis opvallend langer is dan wat andere scans van dezelfde
# route/cabin class laten zien (bv. een lage prijs door een 30 uur durende omweg). Bewust
# GEEN vaste bovengrens: 24 uur naar Sydney is normaal, 24 uur naar Barcelona niet.
MAX_DURATION_FACTOR = 1.5     # t.o.v. de mediane reisduur van de route...
MIN_DURATION_EXCESS = 120     # ...en minstens zoveel minuten langer (tegen ruis bij korte routes)
MIN_DURATIONS_FOR_MEDIAN = 3  # per route; daaronder terugval op dezelfde bestemming vanaf andere luchthavens

# Business Class boven dit bedrag is sowieso geen goede deal, ongeacht de score.
MAX_BUSINESS_PRICE = 2000.0
# Van alle kandidaten (economy + business) mag hooguit dit aandeel Business zijn;
# is er meer Business-materiaal, dan vallen de laagst scorende Business-kandidaten af.
MAX_BUSINESS_SHARE = 0.2

SCORE_FIELDS = ("deal_score", "discount_pct", "drop_pct")
# Puur informatief (tellen niet mee in de score), maar moeten wel ververst worden
# zodra een nieuwere scan van hetzelfde datumpaar iets anders laat zien.
ITINERARY_FIELDS = ("outbound_stops", "outbound_stopover_airports", "outbound_duration_minutes", "outbound_airlines")


# ============================================================
# SCORE
# ============================================================

def _ts(scan):
    return datetime.fromisoformat(scan["scanned_at"])


def _combo(scan):
    # cabin_class erbij: Economy- en Business-scans van dezelfde route/data zijn
    # onafhankelijke prijsreeksen en mogen nooit als 'hetzelfde datumpaar' gelden.
    return (scan["route_id"], scan["depart_date"], scan["return_date"], scan["cabin_class"])


def route_duration_medians(history, dest_by_route=None):
    """
    {(route_id, cabin_class): mediane heenreisduur in minuten}. Heeft een route zelf te weinig
    bekende duren (< MIN_DURATIONS_FOR_MEDIAN), dan geldt de mediaan van dezelfde bestemming
    over alle vertrekluchthavens (`dest_by_route`: {route_id: bestemming}) -- een beter
    vergelijkingspunt dan niets, want de reisduur naar Sydney verschilt weinig per vertrekpunt.
    """
    dest_by_route = dest_by_route or {}
    per_route, per_bestemming = {}, {}
    for s in latest_scan_per_combo(history):
        d = s.get("outbound_duration_minutes")
        if not d:
            continue
        per_route.setdefault((s["route_id"], s["cabin_class"]), []).append(d)
        dest = dest_by_route.get(s["route_id"])
        if dest:
            per_bestemming.setdefault((dest, s["cabin_class"]), []).append(d)
    medianen = {}
    for (route_id, cabin), v in per_route.items():
        if len(v) >= MIN_DURATIONS_FOR_MEDIAN:
            medianen[(route_id, cabin)] = median(v)
    for route_id, dest in dest_by_route.items():
        for cabin in ("economy", "business"):
            v = per_bestemming.get((dest, cabin), [])
            if (route_id, cabin) not in medianen and len(v) >= MIN_DURATIONS_FOR_MEDIAN:
                medianen[(route_id, cabin)] = median(v)
    return medianen


def is_extreme_duration(duur, mediaan):
    """True als `duur` (min.) opvallend langer dan de route-`mediaan` is (geen mediaan: nooit)."""
    if not duur:
        return False
    return bool(mediaan) and duur > mediaan * MAX_DURATION_FACTOR and duur - mediaan >= MIN_DURATION_EXCESS


def pct_below(reference, price):
    """Hoeveel procent `price` onder `reference` ligt (negatief = erboven)."""
    if not reference or reference <= 0:
        return None
    return round((reference - price) / reference * 100, 1)


def latest_scan_per_combo(scans):
    """Houdt per (route, heen, terug) alleen de meest recente scan over."""
    nieuwste = {}
    for s in scans:
        sleutel = _combo(s)
        if sleutel not in nieuwste or _ts(s) > _ts(nieuwste[sleutel]):
            nieuwste[sleutel] = s
    return list(nieuwste.values())


def route_medians(history):
    """
    {(route_id, cabin_class): mediaan van de recentste prijs per datumpaar} (alleen
    bij genoeg datumparen). Per cabin class: Business-prijzen zouden een Economy-
    mediaan (en omgekeerd) zinloos maken.
    """
    prijzen = {}
    for s in latest_scan_per_combo(history):
        prijzen.setdefault((s["route_id"], s["cabin_class"]), []).append(float(s["lowest_price"]))
    return {
        sleutel: median(p)
        for sleutel, p in prijzen.items()
        if len(p) >= MIN_COMBOS_FOR_DISCOUNT
    }


def compute_deal_score(discount_pct, drop_pct, price, price_floor, tier=1):
    """Combineert de losse maten tot één score; None zonder historie."""
    if discount_pct is None and drop_pct is None:
        return None
    score = max(discount_pct or 0, 0) + max(drop_pct or 0, 0)
    if price_floor and price <= float(price_floor) * FLOOR_TOLERANCE:
        score += FLOOR_BONUS
    score += RARITY_BONUS.get(tier, 0.0)
    return round(score, 1)


def score_scan(scan, medians, scans_by_combo, tier=1):
    """Rekent discount_pct, drop_pct en deal_score uit voor één (recentste) scan."""
    prijs = float(scan["lowest_price"])
    mediaan_sleutel = (scan["route_id"], scan["cabin_class"])

    discount_pct = None
    if mediaan_sleutel in medians:
        discount_pct = pct_below(medians[mediaan_sleutel], prijs)

    eerdere = [
        float(h["lowest_price"])
        for h in scans_by_combo[_combo(scan)]
        if _ts(scan) - _ts(h) >= MIN_AGE_FOR_DROP
    ]
    drop_pct = pct_below(median(eerdere), prijs) if eerdere else None

    return {
        "discount_pct": discount_pct,
        "drop_pct": drop_pct,
        "deal_score": compute_deal_score(discount_pct, drop_pct, prijs, scan.get("price_floor"), tier),
        # Puur informatief, overgenomen van de scan zelf (zie ITINERARY_FIELDS hierboven).
        "outbound_stops": scan.get("outbound_stops"),
        "outbound_stopover_airports": scan.get("outbound_stopover_airports"),
        "outbound_duration_minutes": scan.get("outbound_duration_minutes"),
        "outbound_airlines": scan.get("outbound_airlines"),
    }


def best_deal_per_route_and_cabin(history, sinds, tier_by_route=None, dest_by_route=None):
    """
    Per (route, cabin class) de beste deal: hoogste score, bij gelijke stand de
    laagste prijs, dan de vroegste vertrekdatum. Kandidaten zijn datumparen waarvan
    de MEEST RECENTE scan (gescand op/na `sinds`) een deal is; een oude lage prijs
    mag een nieuwere, hogere dus niet overrulen. Elke teruggegeven scan bevat extra
    de sleutels discount_pct, drop_pct, deal_score en de ITINERARY_FIELDS.

    `dest_by_route` ({route_id: bestemming}) laat de reistijd-mediaan terugvallen op de
    bestemming als een route zelf te weinig duren kent.
    `tier_by_route` ({route_id: tier}) levert de RARITY_BONUS; een route die
    ontbreekt (of als het argument wordt weggelaten) telt als tier 1, dus geen
    bonus. Geeft {(route_id, cabin_class): kandidaat} terug.

    Mix-grens: van alle teruggegeven kandidaten mag hooguit MAX_BUSINESS_SHARE
    (20%) Business zijn -- bij een overschot vallen de laagst scorende
    Business-kandidaten af, ook als ze individueel wel boven MIN_SCORE zaten.
    """
    tier_by_route = tier_by_route or {}
    medians = route_medians(history)
    duur_medians = route_duration_medians(history, dest_by_route)
    scans_by_combo = {}
    for s in history:
        scans_by_combo.setdefault(_combo(s), []).append(s)

    beste = {}
    for s in latest_scan_per_combo(history):
        if not s["is_deal"] or _ts(s) < sinds:
            continue
        if s["cabin_class"] == "business" and float(s["lowest_price"]) > MAX_BUSINESS_PRICE:
            continue  # boven de grens is Business sowieso geen goede deal, ongeacht de score
        if is_extreme_duration(s.get("outbound_duration_minutes"),
                               duur_medians.get((s["route_id"], s["cabin_class"]))):
            continue  # extreem lange reistijd voor deze route: geen kandidaat
        tier = tier_by_route.get(s["route_id"], 1)
        kandidaat = {**s, **score_scan(s, medians, scans_by_combo, tier)}
        if kandidaat["deal_score"] is None or kandidaat["deal_score"] < MIN_SCORE:
            continue  # kwaliteitsgrens: geen (aantoonbaar goede) score, geen kandidaat
        echte_korting = max(kandidaat["discount_pct"] or -999, kandidaat["drop_pct"] or -999)
        if echte_korting < MIN_DISCOUNT_PCT:
            continue  # geen waanzinnige deal: te weinig ECHTE korting, ongeacht de bonussen
        rang = (-kandidaat["deal_score"], float(s["lowest_price"]), s["depart_date"])
        sleutel = (s["route_id"], s["cabin_class"])
        huidig = beste.get(sleutel)
        if huidig is None or rang < huidig[0]:
            beste[sleutel] = (rang, kandidaat)

    # Mix-grens: hooguit MAX_BUSINESS_SHARE (20%) van alle kandidaten mag Business zijn.
    # n_economy vast, dus n_business/(n_economy + n_business) <= aandeel
    # <=> n_business <= aandeel / (1 - aandeel) * n_economy. Bij overschot vallen de
    # laagst scorende Business-kandidaten af (zelfde rangorde als hierboven).
    n_economy = sum(1 for sleutel in beste if sleutel[1] == "economy")
    business_sleutels = [sleutel for sleutel in beste if sleutel[1] == "business"]
    max_business = int(n_economy * MAX_BUSINESS_SHARE / (1 - MAX_BUSINESS_SHARE))
    if len(business_sleutels) > max_business:
        business_sleutels.sort(key=lambda sleutel: beste[sleutel][0])
        for sleutel in business_sleutels[max_business:]:
            del beste[sleutel]

    return {sleutel: kandidaat for sleutel, (_, kandidaat) in beste.items()}


# ============================================================
# SCHRIJFPLAN
# ============================================================

def _same(a, b):
    """Gelijk na afronding op 1 decimaal (getallen) of exact (lijsten); None == alleen None."""
    if a is None or b is None:
        return a is b
    if isinstance(a, (list, tuple)) or isinstance(b, (list, tuple)):
        return list(a) == list(b)
    return round(float(a), 1) == round(float(b), 1)


def plan_writes(beste, bestaand):
    """
    Bepaalt wat er naar curated_deals moet:
      rijen       volledige rijen (nieuw of prijs gewijzigd) -> altijd terug naar
                  'wacht op review'
      score_updates  [(id, {score-/itineraryvelden})] voor rijen met dezelfde prijs
                  waarvan alleen die velden zijn veranderd; raakt de review-status
                  niet aan
    Geeft (rijen, score_updates, n_nieuw, n_gewijzigd, n_ongewijzigd) terug.

    `beste` is {(route_id, cabin_class): kandidaat}, zoals best_deal_per_route_and_cabin
    teruggeeft.
    """
    bestaand_per_sleutel = {_combo(r): r for r in bestaand}
    nu = datetime.now(timezone.utc).isoformat()
    rijen, score_updates = [], []
    n_nieuw = n_gewijzigd = n_ongewijzigd = 0
    ververs_velden = SCORE_FIELDS + ITINERARY_FIELDS

    for (route_id, cabin_class), s in beste.items():
        prijs = round(float(s["lowest_price"]), 2)
        ververs = {veld: s[veld] for veld in ververs_velden}
        oud = bestaand_per_sleutel.get(_combo(s))

        # Een nog onbeoordeelde rij die de hercontrole als 'verlopen' markeerde, maar nu
        # opnieuw een deal is (zelfde prijs): terug naar een schone 'wacht op review'.
        # Een door de mens beoordeelde rij blijft ongemoeid.
        verlopen = (
            oud is not None
            and oud.get("recheck_status") == "verlopen"
            and not oud.get("verified_by_human")
        )

        if oud is not None and round(float(oud["price"]), 2) == prijs and not verlopen:
            n_ongewijzigd += 1
            if not all(_same(oud.get(veld), ververs[veld]) for veld in ververs_velden):
                score_updates.append((oud["id"], ververs))
            continue

        rijen.append({
            "route_id": route_id,
            "cabin_class": cabin_class,
            "depart_date": s["depart_date"],
            "return_date": s["return_date"],
            "price": prijs,
            "insight_label": s["insight_label"],
            "origin": s.get("origin"),            # voor de website (die `routes` niet leest)
            "destination": s.get("destination"),
            "generated_at": nu,
            # Blijft staan over prijswijzigingen heen (zie ITINERARY_FIELDS-achtige aanpak, maar dit
            # hoort niet bij "puur informatief ververst": het is bewust het TEGENOVERGESTELDE van
            # generated_at hierboven -- wanneer we deze deal voor het EERST zagen, niet de laatste
            # prijsversie). Nieuw: nu. Prijswijziging van een bestaande rij: overgenomen van `oud`.
            "first_generated_at": (oud.get("first_generated_at") if oud is not None else None) or nu,
            **ververs,
            # Altijd terug naar 'wacht op review': nieuw, of prijs is veranderd.
            "verified_by_human": False,
            "verified_at": None,
            "published": False,
            # Een eerdere hercontrole (3_recheck.py) gold voor de oude situatie.
            "rechecked_at": None,
            "recheck_status": None,
            "recheck_price": None,
            "recheck_label": None,
        })
        if oud is None:
            n_nieuw += 1
        else:
            n_gewijzigd += 1

    return rijen, score_updates, n_nieuw, n_gewijzigd, n_ongewijzigd


# ============================================================
# HOOFDPROGRAMMA
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Scans -> kandidaat-deals in curated_deals")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="hoeveel dagen scans als deal-kandidaat meenemen")
    parser.add_argument("--dry-run", action="store_true", help="alleen tonen, niets schrijven")
    args = parser.parse_args()

    client = get_client()
    nu = datetime.now(timezone.utc)
    sinds = nu - timedelta(days=args.days)
    history_sinds = nu - timedelta(days=max(args.days, HISTORY_DAYS))
    vandaag = date.today().isoformat()

    routes = {r["id"]: r for r in fetch_all(lambda: client.table("routes").select("id, origin, destination"))}
    tier_by_iata = {
        d["iata"]: d["tier"]
        for d in fetch_all(lambda: client.table("destinations").select("iata, tier"), order_by="iata")
    }
    tier_by_route = {route_id: tier_by_iata.get(r["destination"], 4) for route_id, r in routes.items()}

    # Alleen scans mét prijs (fout/geen_data vallen af) en met een vertrekdatum
    # die nog niet voorbij is.
    history = fetch_all(
        lambda: client.table("scans")
        .select("route_id, depart_date, return_date, cabin_class, lowest_price, insight_label, "
                "price_floor, is_deal, scanned_at, "
                "outbound_stops, outbound_stopover_airports, outbound_duration_minutes, outbound_airlines")
        .gte("scanned_at", history_sinds.isoformat())
        .gte("depart_date", vandaag)
        .not_.is_("lowest_price", "null")
    )
    print(f"{len(history)} scans met prijs uit de laatste {max(args.days, HISTORY_DAYS)} dagen "
          f"(deal-kandidaten: laatste {args.days} dagen).")

    beste = best_deal_per_route_and_cabin(
        history, sinds, tier_by_route, {rid: r["destination"] for rid, r in routes.items()})
    print(f"{len(beste)} (route, cabin class)-combinaties met minstens één deal.")
    if not beste:
        return

    for (route_id, _cabin), s in beste.items():
        s["origin"], s["destination"] = routes[route_id]["origin"], routes[route_id]["destination"]
    for (route_id, cabin_class), s in sorted(beste.items(), key=lambda kv: -(kv[1]["deal_score"] or 0)):
        r = routes[route_id]
        score = "geen historie" if s["deal_score"] is None else f"score {s['deal_score']}"
        print(f"  {r['origin']}->{r['destination']} ({cabin_class}) {s['depart_date']}: "
              f"€{float(s['lowest_price']):.0f}, {score} "
              f"(t.o.v. route: {s['discount_pct']}%, gedaald: {s['drop_pct']}%)")

    bestaand = fetch_all(
        lambda: client.table("curated_deals")
        .select("id, route_id, depart_date, return_date, cabin_class, price, deal_score, discount_pct, "
                "drop_pct, outbound_stops, outbound_stopover_airports, outbound_duration_minutes, "
                "outbound_airlines, first_generated_at, verified_by_human, recheck_status")
        .in_("route_id", list({route_id for route_id, _cabin in beste.keys()}))
    )
    rijen, score_updates, n_nieuw, n_gewijzigd, n_ongewijzigd = plan_writes(beste, bestaand)

    print(f"Nieuw: {n_nieuw}, prijs gewijzigd: {n_gewijzigd}, ongewijzigd: {n_ongewijzigd} "
          f"(waarvan {len(score_updates)} met bijgewerkte score).")
    if args.dry_run:
        print("DRY-RUN: niets geschreven.")
        return

    if rijen:
        client.table("curated_deals").upsert(
            rijen, on_conflict="route_id,depart_date,return_date,cabin_class"
        ).execute()
        print(f"{len(rijen)} rijen geschreven (published = false). "
              f"Ga verder met: python 3_recheck.py, daarna 4_review.py")
    # Aparte update per rij, alleen de score-/itinerarykolommen: een gemengde
    # partial-upsert zou ontbrekende kolommen (o.a. published) kunnen overschrijven.
    for deal_id, velden in score_updates:
        client.table("curated_deals").update(velden).eq("id", deal_id).execute()
    if not rijen and not score_updates:
        print("Niets te schrijven.")


if __name__ == "__main__":
    main()
