"""
Gedeelde onderdelen van de stappen die deals opnieuw scannen (3_recheck.py, 5_livecheck.py).

Alles wat met scraping-etiquette te maken heeft, staat op één plek: nooit twee scrapers tegelijk
(lock, gedeeld met scripts/run_daily.sh), één browsersessie, 4-9 s pauze, en stoppen bij een
blokkade. 1_scanner.py heeft eigen logica (het rooster van combinaties) en gebruikt dit niet.
"""

import os
import random
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

from lib.db import fetch_all

from lib.scraper import (
    MAX_CONSECUTIVE_ERRORS,
    SEARCH_DELAY_MAX_SECONDS,
    SEARCH_DELAY_MIN_SECONDS,
    search_route_insights,
)

LOCK = Path(__file__).resolve().parent.parent / "logs" / ".lock"   # public repo: lib/ staat in de root (in de private repo: analytics/lib/)

# scripts/run_daily.sh houdt de lock zelf vast en start ons daaronder. Het zet zijn eigen pid in deze
# omgevingsvariabele; klopt die met de pid in de lock, dan lenen we de lock in plaats van te weigeren.
LOCK_PID_ENV = "SCRAPER_LOCK_PID"
_lock_geleend = False


def is_recent(timestamp_iso, nu, within):
    """True als `timestamp_iso` (of None) minder dan `within` (timedelta) voor `nu` ligt."""
    if not timestamp_iso:
        return False
    return nu - datetime.fromisoformat(timestamp_iso) < within


def deadline_na(minuten):
    """Moment (time.monotonic) waarop scan_each moet stoppen, of None zonder limiet."""
    return None if minuten is None else time.monotonic() + minuten * 60


def verse_scans(client, deals, binnen, nu):
    """
    Zoekt voor elke deal de nieuwste échte scan (met label, dus geen kaal kalenderraster-rijtje) van
    exact dezelfde route, datums en cabin class die hooguit `binnen` (timedelta) oud is. De scanner
    heeft veel deals zojuist al gescand: die hoeven niet nog eens op Google Flights opgehaald.
    Geeft {deal["id"]: scanrij}; een deal zonder verse scan ontbreekt.
    """
    if not deals:
        return {}
    route_ids = sorted({d["route_id"] for d in deals})
    rijen = fetch_all(
        lambda: client.table("scans")
        .select("route_id, depart_date, return_date, cabin_class, scanned_at, lowest_price, "
                "insight_label, price_floor, is_deal")
        .in_("route_id", route_ids)
        .gte("scanned_at", (nu - binnen).isoformat())
        .in_("insight_label", ["laag", "gemiddeld", "hoog", "onbekend", "geen_data"])
    )
    nieuwste = {}
    for r in rijen:
        sleutel = (r["route_id"], r["depart_date"], r["return_date"], r["cabin_class"])
        if sleutel not in nieuwste or r["scanned_at"] > nieuwste[sleutel]["scanned_at"]:
            nieuwste[sleutel] = r
    uit = {}
    for d in deals:
        r = nieuwste.get((d["route_id"], d["depart_date"], d["return_date"], d["cabin_class"]))
        if r:
            uit[d["id"]] = r
    return uit


def take_lock():
    global _lock_geleend
    LOCK.parent.mkdir(exist_ok=True)

    van_ouder = os.environ.get(LOCK_PID_ENV, "").strip()
    if van_ouder and LOCK.is_dir():
        try:
            if (LOCK / "pid").read_text().strip() == van_ouder:
                _lock_geleend = True     # de ouder is eigenaar en ruimt zelf op
                return True
        except FileNotFoundError:
            pass

    try:
        LOCK.mkdir()
    except FileExistsError:
        try:
            oude_pid = int((LOCK / "pid").read_text().strip())
            os.kill(oude_pid, 0)
            print(f"Er draait al een scanner/hercontrole (pid {oude_pid}); deze start niet.")
            return False
        except (ValueError, FileNotFoundError, ProcessLookupError):
            shutil.rmtree(LOCK, ignore_errors=True)   # lock van een gecrasht proces
            LOCK.mkdir()
        except PermissionError:
            print("Er draait al een scanner/hercontrole; deze start niet.")
            return False
    (LOCK / "pid").write_text(str(os.getpid()))
    return True


def release_lock():
    global _lock_geleend
    if _lock_geleend:            # geleende lock: die is van de ouder (run_daily.sh)
        _lock_geleend = False
        return
    shutil.rmtree(LOCK, ignore_errors=True)


def scan_each(items, target, announce=None, deadline=None):
    """
    Scant de items één voor één op Google Flights en geeft per item (item, insights) terug;
    insights is None als de scan na alle retries mislukte.

    target(item)   -> (origin, destination, depart_date, return_date)
    announce(i, n, item)  optioneel: wordt vlak vóór elke scan aangeroepen (voortgang printen)
    deadline       optioneel (time.monotonic-waarde, zie deadline_na): na de lopende scan stoppen
                   we netjes, zodat de stappen erna in de pipeline nog tijd hebben

    Houdt de scraping-etiquette aan (zie boven). Bij MAX_CONSECUTIVE_ERRORS mislukkingen achter
    elkaar stopt het: dan zijn we waarschijnlijk geblokkeerd en maakt doorgaan het erger.
    """
    opeenvolgende_fouten = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(locale="nl-NL")   # 1 sessie: de consent-cookie blijft behouden
            for i, item in enumerate(items, start=1):
                if deadline is not None and time.monotonic() >= deadline:
                    print(f"STOP: tijdsbudget op na {i - 1}/{len(items)} deals; de rest komt de volgende run.")
                    return
                if announce:
                    announce(i, len(items), item)
                insights = search_route_insights(page, *target(item))
                opeenvolgende_fouten = opeenvolgende_fouten + 1 if insights is None else 0
                yield item, insights

                if opeenvolgende_fouten >= MAX_CONSECUTIVE_ERRORS:
                    print(f"STOP: {MAX_CONSECUTIVE_ERRORS} mislukte pageloads achter elkaar -- "
                          f"waarschijnlijk geblokkeerd. Probeer het later opnieuw.")
                    return
                if i < len(items):
                    time.sleep(random.uniform(SEARCH_DELAY_MIN_SECONDS, SEARCH_DELAY_MAX_SECONDS))
        finally:
            browser.close()
