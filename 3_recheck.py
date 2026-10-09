"""
Stap 3: hercontrole van kandidaat-deals, vlak vóór de menselijke review
=======================================================================

Een deal kan tussen scan en review alweer verdwenen zijn (tijdelijk tarief,
prijsschommeling). Deze stap scant elke deal die op review wacht daarom nog één
keer, met exact dezelfde zoekopdracht en uitleesmethode als de scanner, en legt
de uitkomst vast op de rij in `curated_deals`:

  ok        nog steeds 'laag': de prijs op de rij wordt bijgewerkt naar de actuele
            prijs, zodat je in de review niet een verouderd bedrag beoordeelt
  verlopen  niet meer 'laag', of geen prijs meer: review.py verbergt de deal
            standaard (met --alles zie je hem toch)

Een mislukte pageload (netwerk/blokkade) verandert niets aan de rij: de deal blijft
'niet hercontroleerd' en review.py waarschuwt daarvoor.

Deze stap zet NOOIT `published` of `verified_by_human`: afwijzen en goedkeuren
blijft een menselijke beslissing in 4_review.py. Elke hercontrole wordt
ook als gewone scan opgeslagen (nieuwe scanhistorie; een niet-meer-'laag'-scan
maakt de combinatie ook voor curate.py geen kandidaat meer).

Scraping-etiquette (zie lib/scraper.py): één deal per keer, 4-9 s pauze, één
browsersessie, en nooit tegelijk met de scanner (gedeelde lock in logs/.lock).

Gebruik (vanuit de projectroot):
    python 3_recheck.py               # alle wachtende deals (niet <6 uur geleden gecontroleerd)
    python 3_recheck.py --limit 5     # alleen de 5 beste (hoogste score)
    python 3_recheck.py --force       # ook deals die net gecontroleerd zijn
    python 3_recheck.py --dry-run     # scrapen, maar niets schrijven
"""

import argparse
from datetime import date, datetime, timedelta, timezone

from lib.db import fetch_all, get_client
from lib.scanrun import deadline_na, is_recent, release_lock, scan_each, take_lock, verse_scans
from lib.scraper import build_scan_row, save_scan

SKIP_IF_RECHECKED_WITHIN = timedelta(hours=6)
# Een scan van de scanner die hooguit zo oud is telt als hercontrole: niet nog eens ophalen
# (zelfde grens als SKIP_IF_RECHECKED_WITHIN: een 6 uur oude hercontrole telt immers ook).
HERGEBRUIK_SCAN_BINNEN = timedelta(hours=6)


# ============================================================
# BEOORDELING (puur, getest in tests/test_recheck.py)
# ============================================================

def beoordeel(scan_row, nu):
    """
    Zet een scanrij (uit build_scan_row) om naar de update voor curated_deals.
    None als de scan zelf mislukte: dan blijft de deal ongemoeid.
    """
    if scan_row["insight_label"] == "fout":
        return None

    update = {
        "rechecked_at": nu.isoformat(),
        "recheck_price": scan_row["lowest_price"],
        "recheck_label": scan_row["insight_label"],
    }
    if scan_row["is_deal"] and scan_row["lowest_price"] is not None:
        update["recheck_status"] = "ok"
        update["price"] = scan_row["lowest_price"]   # actuele prijs, niet de verouderde
    else:
        update["recheck_status"] = "verlopen"
    return update


def is_vers_genoeg(deal, nu):
    """True als de deal onlangs al is hercontroleerd (dan slaan we hem over)."""
    return is_recent(deal.get("rechecked_at"), nu, SKIP_IF_RECHECKED_WITHIN)


# ============================================================
# HOOFDPROGRAMMA
# ============================================================

def load_pending(client):
    """Alle deals die op review wachten (vertrekdatum nog niet voorbij), beste score eerst."""
    rijen = fetch_all(
        lambda: client.table("curated_deals")
        .select("id, route_id, depart_date, return_date, price, deal_score, cabin_class, rechecked_at, "
                "routes(origin, destination)")
        .eq("verified_by_human", False)
        .gte("depart_date", date.today().isoformat())
    )
    rijen.sort(key=lambda d: (d["deal_score"] is None, -(d["deal_score"] or 0), float(d["price"])))
    return rijen


def main():
    parser = argparse.ArgumentParser(description="Scan wachtende deals nog één keer vóór de review")
    parser.add_argument("--limit", type=int, help="alleen de N beste deals (hoogste score) hercontroleren")
    parser.add_argument("--force", action="store_true", help="ook deals die < 6 uur geleden zijn gecontroleerd")
    parser.add_argument("--max-minuten", type=float,
                        help="stop netjes na de lopende deal als dit aantal minuten om is")
    parser.add_argument("--dry-run", action="store_true", help="scrapen, maar niets naar Supabase schrijven")
    args = parser.parse_args()

    client = get_client()
    nu = datetime.now(timezone.utc)

    wachtend = load_pending(client)
    te_doen = [d for d in wachtend if args.force or not is_vers_genoeg(d, nu)]
    n_vers = len(wachtend) - len(te_doen)
    if args.limit is not None:
        te_doen = te_doen[:args.limit]
    print(f"{len(wachtend)} deal(s) wachten op review; {len(te_doen)} worden hercontroleerd "
          f"(~{len(te_doen) * 16 / 60:.0f} min)"
          + (f", {n_vers} overgeslagen (recent gecontroleerd)." if n_vers else "."))
    if not te_doen:
        return
    if args.dry_run:
        print("DRY-RUN: er wordt niets naar Supabase geschreven.")

    n_ok = n_verlopen = n_fout = 0

    def verwerk(deal, scan_row, beoordeeld_op, opslaan):
        """Beoordeelt één deal aan de hand van een scanrij en schrijft het besluit weg."""
        nonlocal n_ok, n_verlopen, n_fout
        update = beoordeel(scan_row, beoordeeld_op)
        if update is None:
            n_fout += 1
            print("  Mislukt: deal blijft 'niet hercontroleerd'.\n")
            return

        prijs = "geen prijs" if update["recheck_price"] is None else f"€{update['recheck_price']:.0f}"
        if update["recheck_status"] == "ok":
            n_ok += 1
            print(f"  OK: nog steeds '{update['recheck_label']}', {prijs}.\n")
        else:
            n_verlopen += 1
            print(f"  VERLOPEN: nu {prijs}, '{update['recheck_label']}'.\n")

        if not args.dry_run:
            if opslaan:
                save_scan(client, scan_row)
            # verified_by_human = false: nooit een intussen beoordeelde rij overschrijven
            client.table("curated_deals").update(update).eq("id", deal["id"]) \
                .eq("verified_by_human", False).execute()

    # Eerst de deals waarvan de scanner zelf net een verse scan heeft: geen nieuwe pageload nodig.
    hergebruik = verse_scans(client, te_doen, HERGEBRUIK_SCAN_BINNEN, nu)
    if hergebruik:
        print(f"{len(hergebruik)} deal(s) hebben een scan van de afgelopen "
              f"{HERGEBRUIK_SCAN_BINNEN.total_seconds() / 3600:g} uur: die tellen als hercontrole.\n")
    for deal in te_doen:
        scan = hergebruik.get(deal["id"])
        if scan is None:
            continue
        print(f"[hergebruik] {deal['routes']['origin']} -> {deal['routes']['destination']} "
              f"({deal['cabin_class']}), heen {deal['depart_date']}, terug {deal['return_date']} "
              f"(was €{float(deal['price']):.0f})")
        verwerk(deal, scan, datetime.fromisoformat(scan["scanned_at"]), opslaan=False)

    te_scannen = [d for d in te_doen if d["id"] not in hergebruik]
    if te_scannen:
        if not take_lock():
            return
        try:
            for deal, insights in scan_each(
                te_scannen,
                target=lambda d: (d["routes"]["origin"], d["routes"]["destination"], d["depart_date"],
                                   d["return_date"], d["cabin_class"]),
                announce=lambda i, n, d: print(
                    f"[{i}/{n}] {d['routes']['origin']} -> {d['routes']['destination']} ({d['cabin_class']}), "
                    f"heen {d['depart_date']}, terug {d['return_date']} (was €{float(d['price']):.0f})"),
                deadline=deadline_na(args.max_minuten),
            ):
                scan_row = build_scan_row(deal["route_id"], deal["depart_date"], deal["return_date"], insights,
                                           cabin_class=deal["cabin_class"])
                verwerk(deal, scan_row, datetime.now(timezone.utc), opslaan=True)
        finally:
            release_lock()

    print(f"=== Klaar. {n_ok} ok, {n_verlopen} verlopen, {n_fout} mislukt. "
          f"Ga verder met: python 4_review.py ===")


if __name__ == "__main__":
    main()
