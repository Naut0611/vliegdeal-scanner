"""
Stap 5: controle van de deals die NU LIVE staan
================================================

Een goedgekeurde deal blijft op de site tot de vertrekdatum voorbij is, ook als de prijs intussen
gestegen of verdwenen is. Deze stap scant elke live deal nog eens, met dezelfde zoekopdracht en
uitleesmethode als de scanner, en beslist:

  ok        nog steeds 'laag' EN de prijs is niet meer dan --marge % gestegen t.o.v. de prijs op
            de site: de deal blijft live en de prijs wordt bijgewerkt naar de actuele prijs
  verlopen  niet meer 'laag', geen prijs meer, of te ver gestegen: de deal gaat van de site af
            (published = false) en terug naar 'wacht op review', gemarkeerd als 'verlopen'

De marge vangt normale prijsschommelingen op, zodat een deal niet verdwijnt omdat hij 2% duurder
werd. Een deal die daarna toch weer scherp is, komt via 2_curate.py vanzelf opnieuw in de review
(4_review.py --alles toont verlopen deals).

Deze stap kan alleen deals van de site HALEN, nooit erop zetten: `published` wordt hier nooit
true. Een mislukte pageload (netwerk/blokkade) verandert niets: in twijfel blijft een deal staan.

Scraping-etiquette: zie lib/scanrun.py (lock, één deal per keer, 4-9 s pauze, stop bij blokkade).

Gebruik (vanuit de projectroot):
    python 5_livecheck.py                 # alle live deals (niet < 6 uur geleden gecontroleerd)
    python 5_livecheck.py --marge 3       # strenger: max. 3% duurder
    python 5_livecheck.py --dry-run       # scrapen en tonen wat er zou gebeuren, niets schrijven
    python 5_livecheck.py --force         # ook deals die net gecontroleerd zijn
"""

import argparse
from datetime import date, datetime, timedelta, timezone

from lib.db import fetch_all, get_client
from lib.scanrun import deadline_na, is_recent, release_lock, scan_each, take_lock, verse_scans
from lib.scraper import build_scan_row, save_scan

DEFAULT_MARGE_PCT = 5.0
MAX_MARGE_PCT = 50.0
SKIP_IF_CHECKED_WITHIN = timedelta(hours=6)
# Een scan van de scanner die hooguit zo oud is telt als controle: niet nog eens ophalen.
HERGEBRUIK_SCAN_BINNEN = timedelta(hours=6)


# ============================================================
# BEOORDELING (puur, getest in tests/test_livecheck.py)
# ============================================================

def prijsgrens(huidige_prijs, marge):
    """Hoogste prijs die nog binnen de marge valt, in hele centen. marge = 0,05 voor 5%."""
    return round(float(huidige_prijs) * (1 + marge), 2)


def beoordeel_live(scan_row, huidige_prijs, marge, nu):
    """
    Beslist over één live deal aan de hand van de nieuwe scan (uit build_scan_row).

    Geeft None als de scan zelf mislukte (dan blijft alles ongemoeid), anders
    {"status": "ok" | "verlopen", "reden": str | None, "update": {kolommen voor curated_deals}}.
    De update zet `published` nooit op true.
    """
    if scan_row["insight_label"] == "fout":
        return None

    nieuw = scan_row["lowest_price"]
    update = {
        "rechecked_at": nu.isoformat(),
        "recheck_price": nieuw,
        "recheck_label": scan_row["insight_label"],
    }
    grens = prijsgrens(huidige_prijs, marge)

    if nieuw is None:
        reden = "geen prijs meer gevonden"
    elif not scan_row["is_deal"]:
        reden = f"Google noemt de prijs niet meer 'laag' maar '{scan_row['insight_label']}'"
    elif float(nieuw) > grens:
        reden = f"€{float(nieuw):.0f} ligt boven de grens van €{grens:.0f} (marge {marge * 100:g}%)"
    else:
        return {"status": "ok", "reden": None,
                "update": {**update, "recheck_status": "ok", "price": nieuw}}   # actuele prijs tonen

    return {"status": "verlopen", "reden": reden, "update": {
        **update,
        "recheck_status": "verlopen",
        # Van de site af en terug naar 'wacht op review'. De prijs blijft die van de laatste goedkeuring.
        "published": False,
        "verified_by_human": False,
        "verified_at": None,
    }}


def is_vers_genoeg(deal, nu):
    """True als de deal onlangs al is gecontroleerd (dan slaan we hem over)."""
    return is_recent(deal.get("rechecked_at"), nu, SKIP_IF_CHECKED_WITHIN)


# ============================================================
# HOOFDPROGRAMMA
# ============================================================

def load_live(client):
    """Alle live deals (vertrekdatum nog niet voorbij); het langst niet gecontroleerd eerst."""
    rijen = fetch_all(
        lambda: client.table("curated_deals")
        .select("id, route_id, depart_date, return_date, price, cabin_class, rechecked_at, "
                "routes(origin, destination)")
        .eq("published", True)
        .eq("verified_by_human", True)
        .gte("depart_date", date.today().isoformat())
    )
    rijen.sort(key=lambda d: (d["rechecked_at"] or "", d["depart_date"]))
    return rijen


def main():
    parser = argparse.ArgumentParser(description="Controleer of de live deals nog steeds goedkoop zijn")
    parser.add_argument("--marge", type=float, default=DEFAULT_MARGE_PCT,
                        help=f"toegestane prijsstijging in procenten (standaard {DEFAULT_MARGE_PCT:g})")
    parser.add_argument("--limit", type=int, help="alleen de N deals die het langst niet gecontroleerd zijn")
    parser.add_argument("--force", action="store_true", help="ook deals die < 6 uur geleden zijn gecontroleerd")
    parser.add_argument("--max-minuten", type=float,
                        help="stop netjes na de lopende deal als dit aantal minuten om is")
    parser.add_argument("--dry-run", action="store_true", help="scrapen en tonen, maar niets schrijven")
    args = parser.parse_args()
    if not 0 <= args.marge <= MAX_MARGE_PCT:
        parser.error(f"--marge moet tussen 0 en {MAX_MARGE_PCT:g} procent liggen")
    marge = args.marge / 100

    client = get_client()
    nu = datetime.now(timezone.utc)

    live = load_live(client)
    te_doen = [d for d in live if args.force or not is_vers_genoeg(d, nu)]
    n_vers = len(live) - len(te_doen)
    if args.limit is not None:
        te_doen = te_doen[:args.limit]
    print(f"{len(live)} deal(s) staan live; {len(te_doen)} worden gecontroleerd (marge {args.marge:g}%, "
          f"~{len(te_doen) * 16 / 60:.0f} min)"
          + (f", {n_vers} overgeslagen (recent gecontroleerd)." if n_vers else "."))
    if not te_doen:
        return
    if args.dry_run:
        print("DRY-RUN: er wordt niets naar Supabase geschreven.")

    n_ok = n_verlopen = n_fout = 0

    def verwerk(deal, scan_row, beoordeeld_op, opslaan):
        """Beoordeelt één deal aan de hand van een scanrij en schrijft het besluit weg."""
        nonlocal n_ok, n_verlopen, n_fout
        besluit = beoordeel_live(scan_row, deal["price"], marge, beoordeeld_op)
        if besluit is None:
            n_fout += 1
            print("  Mislukt: de deal blijft ongewijzigd staan.\n")
            return

        nieuw = besluit["update"]["recheck_price"]
        if besluit["status"] == "ok":
            n_ok += 1
            verschil = (float(nieuw) / float(deal["price"]) - 1) * 100
            print(f"  OK: nog steeds '{besluit['update']['recheck_label']}', €{float(nieuw):.0f} "
                  f"({verschil:+.1f}%); blijft live met de actuele prijs.\n")
        else:
            n_verlopen += 1
            nu_prijs = "geen prijs" if nieuw is None else f"€{float(nieuw):.0f}"
            print(f"  VERLOPEN: {besluit['reden']} (nu {nu_prijs}); "
                  f"{'zou' if args.dry_run else 'gaat'} van de site af.\n")

        if not args.dry_run:
            if opslaan:
                save_scan(client, scan_row)
            # Alleen als de rij nog steeds live is: nooit een intussen gewijzigde rij overschrijven.
            client.table("curated_deals").update(besluit["update"]).eq("id", deal["id"]) \
                .eq("published", True).eq("verified_by_human", True).execute()

    # Eerst de deals waarvan de scanner zelf net een verse scan heeft: geen nieuwe pageload nodig.
    hergebruik = verse_scans(client, te_doen, HERGEBRUIK_SCAN_BINNEN, nu)
    if hergebruik:
        print(f"{len(hergebruik)} deal(s) hebben een scan van de afgelopen "
              f"{HERGEBRUIK_SCAN_BINNEN.total_seconds() / 3600:g} uur: die worden daaruit beoordeeld.\n")
    for deal in te_doen:
        scan = hergebruik.get(deal["id"])
        if scan is None:
            continue
        print(f"[hergebruik] {deal['routes']['origin']} -> {deal['routes']['destination']} "
              f"({deal['cabin_class']}), heen {deal['depart_date']}, terug {deal['return_date']} "
              f"(live voor €{float(deal['price']):.0f})")
        scan_row = {**scan, "stay_days": None}
        verwerk(deal, scan_row, datetime.fromisoformat(scan["scanned_at"]), opslaan=False)

    te_scannen = [d for d in te_doen if d["id"] not in hergebruik]
    n_gescand = 0
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
                    f"heen {d['depart_date']}, terug {d['return_date']} (live voor €{float(d['price']):.0f})"),
                deadline=deadline_na(args.max_minuten),
            ):
                scan_row = build_scan_row(deal["route_id"], deal["depart_date"], deal["return_date"], insights,
                                           cabin_class=deal["cabin_class"])
                n_gescand += 1
                verwerk(deal, scan_row, datetime.now(timezone.utc), opslaan=True)
        finally:
            release_lock()

    print(f"=== Klaar. {n_ok} blijven live, {n_verlopen} verlopen{' (dry-run: niets aangepast)' if args.dry_run else ''}, "
          f"{n_fout} mislukt. ===")
    n_overgebleven = len(te_scannen) - n_gescand
    if n_overgebleven:
        # ::warning:: wordt in GitHub Actions een zichtbare annotatie op de run
        print(f"::warning::{n_overgebleven} live deal(s) zijn niet gecontroleerd (tijdsbudget of blokkade); "
              f"ze staan nog op de site en zijn morgen als eerste aan de beurt.")
    if n_verlopen and not args.dry_run:
        print("Verlopen deals staan niet meer op de site; zie ze met: python 4_review.py --alles")


if __name__ == "__main__":
    main()
