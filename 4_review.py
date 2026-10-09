"""
Stap 4: review -- kandidaat-deals goedkeuren of afwijzen

Interactief (handmatig, standaard) toont dit elke rij in `curated_deals` die nog
op review wacht (verified_by_human = false, vertrekdatum nog niet voorbij), de
hoogste deal-score eerst, en vraagt per rij:

    [g] goedkeuren -> verified_by_human = true,  published = true   (live op de site)
    [a] afwijzen   -> verified_by_human = true,  published = false  (blijft weg)
    [s] overslaan  -> niets veranderen, de rij komt de volgende keer terug
    [q] stoppen

Controleer de deal ZELF via de getoonde Google Flights-link voordat je
goedkeurt: dit is de enige plek waar `published` op true wordt gezet.

Draai eerst `python 3_recheck.py`: die scant elke wachtende deal nog één
keer. Deals die daarbij 'verlopen' bleken (niet meer 'laag') worden hier standaard
verborgen; deals die niet hercontroleerd zijn krijgen een waarschuwing.

Gebruik (vanuit de projectroot):
    python 4_review.py
    python 4_review.py --alles              # toon ook deals die na hercontrole verlopen zijn
    python 4_review.py --automatisch        # keur alles goed zonder los te vragen (zie hieronder)
    python 4_review.py --automatisch --zonder-bevestiging   # zoals hierboven, zonder prompt (voor cron/launchd)

--automatisch keurt een deal goed ZONDER dat iemand hem bekijkt. Dit is de
bewust gekozen, structurele uitzondering op de regel in CLAUDE.md dat elke
gepubliceerde deal een menselijke blik moet hebben gehad (zie de sectie
"Belangrijke randvoorwaarden" daar: het non-negotiable is niet "nooit
automatisch publiceren" maar "nooit publiceren zonder de strikte,
recheck-gebaseerde poort hieronder"). Als rem: alleen deals met een geslaagde
hercontrole die in DEZELFDE run vlak hiervoor is uitgevoerd (recheck_status ==
"ok") worden automatisch goedgekeurd; alle andere blijven gewoon op review
staan voor een handmatige blik. Interactief (zonder --zonder-bevestiging)
wordt bij het opstarten eenmalig om bevestiging gevraagd; scripts/run_daily.sh
gebruikt --zonder-bevestiging omdat daar niemand zit te typen.
"""

import argparse
from datetime import date, datetime, timezone

import fast_flights as ff

from lib.db import fetch_all, get_client


def google_flights_url(origin, destination, depart_date, return_date, seat="economy"):
    """Zoeklink voor de exacte periode (zelfde parameters als de scanner)."""
    query = ff.create_query(
        flights=[
            ff.FlightQuery(date=depart_date, from_airport=origin, to_airport=destination),
            ff.FlightQuery(date=return_date, from_airport=destination, to_airport=origin),
        ],
        trip="round-trip",
        seat=seat,
        passengers=ff.Passengers(adults=1),
        language="nl",
        currency="EUR",
    )
    return query.url()


def sort_pending(deals):
    """Hoogste deal_score eerst; deals zonder score (nog geen historie) achteraan, goedkoopste eerst."""
    return sorted(deals, key=lambda d: (d["deal_score"] is None, -(d["deal_score"] or 0), float(d["price"])))


def describe_score(deal):
    """Leesbare uitleg van de score, bijv. '32% onder route-mediaan, 12% gedaald'."""
    delen = []
    if deal.get("discount_pct") is not None:
        delen.append(f"{deal['discount_pct']:g}% t.o.v. route-mediaan")
    if deal.get("drop_pct") is not None:
        delen.append(f"{deal['drop_pct']:g}% t.o.v. eerdere scans")
    score = "geen historie" if deal.get("deal_score") is None else f"score {deal['deal_score']:g}"
    return f"{score}" + (f" ({', '.join(delen)})" if delen else "")


def describe_outbound(deal):
    """
    Leesbare heenreis-info (maatschappij + tussenstops + duur), bijv. 'KLM, 1 tussenstop
    via ZRH, 14u05 heenreis'. Uitdrukkelijk alleen de heenreis (zie lib/scraper.py) --
    'onbekend' als de itinerary-parsing voor deze scan niets opleverde.
    """
    stops = deal.get("outbound_stops")
    if stops is None:
        return "onbekend"
    if stops == 0:
        route = "direct"
    else:
        via = ", ".join(deal.get("outbound_stopover_airports") or [])
        noun = "tussenstop" if stops == 1 else "tussenstops"
        route = f"{stops} {noun}" + (f" via {via}" if via else "")
    duur = deal.get("outbound_duration_minutes")
    met_duur = route if duur is None else f"{route}, {int(duur) // 60}u{int(duur) % 60:02d} heenreis"
    maatschappij = ", ".join(deal.get("outbound_airlines") or [])
    return f"{maatschappij}, {met_duur}" if maatschappij else met_duur


def describe_recheck(deal):
    """Uitkomst van 3_recheck.py voor in de review, bijv. 'OK: nog steeds 'laag', €373 (21-09 09:02)'."""
    status = deal.get("recheck_status")
    if status is None:
        return "NIET hercontroleerd (de prijs kan inmiddels verlopen zijn; draai 3_recheck.py)"
    wanneer = datetime.fromisoformat(deal["rechecked_at"]).astimezone().strftime("%d-%m %H:%M")
    prijs = "geen prijs meer" if deal.get("recheck_price") is None else f"€{float(deal['recheck_price']):.0f}"
    if status == "ok":
        return f"OK: nog steeds '{deal['recheck_label']}', {prijs} ({wanneer})"
    return f"VERLOPEN: nu {prijs}, '{deal['recheck_label']}' ({wanneer})"


def set_review_result(client, deal_id, goedgekeurd):
    """
    Legt de beslissing vast. De extra filter verified_by_human = false voorkomt
    dat een rij die intussen (elders) al beoordeeld is per ongeluk wordt
    overschreven. Geeft True terug als er echt een rij is bijgewerkt.
    """
    resp = (
        client.table("curated_deals")
        .update({
            "verified_by_human": True,
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "published": goedgekeurd,
        })
        .eq("id", deal_id)
        .eq("verified_by_human", False)
        .execute()
    )
    return len(resp.data) == 1


def ask(prompt):
    """Vraagt om een keuze; None bij Ctrl-C/Ctrl-D (behandeld als stoppen)."""
    try:
        return input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def main():
    parser = argparse.ArgumentParser(description="Kandidaat-deals goedkeuren of afwijzen")
    parser.add_argument("--alles", action="store_true",
                        help="toon ook deals die na hercontrole verlopen zijn")
    parser.add_argument("--automatisch", action="store_true",
                        help="keur alles automatisch goed (alleen deals met recheck_status == 'ok'); "
                             "zie de module-docstring")
    parser.add_argument("--zonder-bevestiging", action="store_true",
                        help="sla de interactieve bevestigingsprompt van --automatisch over "
                             "(voor gebruik in scripts/run_daily.sh via cron/launchd)")
    args = parser.parse_args()

    if args.zonder_bevestiging and not args.automatisch:
        parser.error("--zonder-bevestiging heeft alleen effect samen met --automatisch")

    if args.automatisch:
        print(
            "--automatisch: deals worden goedgekeurd zonder dat iemand ze bekijkt. Alleen deals met "
            "een geslaagde hercontrole in deze run (recheck_status == 'ok') worden meegenomen; de "
            "rest blijft gewoon op review staan."
        )
        if not args.zonder_bevestiging and ask("Typ JA om door te gaan: ") != "ja":
            print("Gestopt.")
            return

    client = get_client()
    vandaag = date.today().isoformat()

    wachtend = fetch_all(
        lambda: client.table("curated_deals")
        .select("id, depart_date, return_date, price, insight_label, generated_at, first_generated_at, "
                "cabin_class, deal_score, discount_pct, drop_pct, "
                "outbound_stops, outbound_stopover_airports, outbound_duration_minutes, outbound_airlines, "
                "rechecked_at, recheck_status, recheck_price, recheck_label, "
                "routes(origin, destination)")
        .eq("verified_by_human", False)
        .gte("depart_date", vandaag)
    )
    if not wachtend:
        print("Geen deals die op review wachten.")
        return

    n_verlopen = sum(1 for d in wachtend if d["recheck_status"] == "verlopen")
    n_niet_gecheckt = sum(1 for d in wachtend if d["recheck_status"] is None)
    if not args.alles:
        wachtend = [d for d in wachtend if d["recheck_status"] != "verlopen"]
    if n_verlopen:
        print(f"{n_verlopen} deal(s) zijn na hercontrole verlopen"
              + (" (hieronder meegenomen, want --alles)." if args.alles else " en worden verborgen (--alles toont ze)."))
    if n_niet_gecheckt:
        print(f"LET OP: {n_niet_gecheckt} deal(s) zijn niet hercontroleerd; draai eerst "
              f"`python 3_recheck.py` als je zeker wilt weten dat de prijs nog klopt.")
    if not wachtend:
        print("Geen deals over om te beoordelen.")
        return

    wachtend = sort_pending(wachtend)
    print(f"{len(wachtend)} deal(s) wachten op review (beste score eerst).\n")
    n_goed = n_af = 0

    for i, deal in enumerate(wachtend, start=1):
        origin, destination = deal["routes"]["origin"], deal["routes"]["destination"]
        print(f"--- Deal {i}/{len(wachtend)} " + "-" * 40)
        print(f"  Route:   {origin} -> {destination} ({deal['cabin_class']})")
        print(f"  Periode: {deal['depart_date']} t/m {deal['return_date']}")
        print(f"  Prijs:   €{float(deal['price']):.0f}  (Google: '{deal['insight_label']}')")
        print(f"  Score:   {describe_score(deal)}")
        print(f"  Heenreis: {describe_outbound(deal)}")
        print(f"  Recheck: {describe_recheck(deal)}")
        print(f"  Gemaakt: {deal['generated_at']}")
        print(f"  Gevonden: {deal.get('first_generated_at') or 'onbekend'}")
        print(f"  Check:   {google_flights_url(origin, destination, deal['depart_date'], deal['return_date'], seat=deal['cabin_class'])}")

        if args.automatisch:
            if deal["recheck_status"] != "ok":
                print("  -> Overgeslagen (geen geslaagde hercontrole): blijft op review staan.\n")
                continue
            keuze = "g"
        else:
            while True:
                keuze = ask("  [g]oedkeuren / [a]fwijzen / [s] overslaan / [q] stoppen: ")
                if keuze in ("g", "a", "s", "q", None):
                    break
                print("  Onbekende keuze.")

            if keuze in ("q", None):
                break
            if keuze == "s":
                print()
                continue

        if set_review_result(client, deal["id"], goedgekeurd=(keuze == "g")):
            if keuze == "g":
                n_goed += 1
                print("  -> Goedgekeurd en gepubliceerd.\n")
            else:
                n_af += 1
                print("  -> Afgewezen.\n")
        else:
            print("  -> Niet bijgewerkt: deze rij was al beoordeeld.\n")

    print(f"Klaar: {n_goed} goedgekeurd, {n_af} afgewezen.")


if __name__ == "__main__":
    main()
