"""
Bestemmingenlijst inladen uit analytics/destinations.csv.

Kolommen: iata, naam, land (ISO-2), deals_per_jaar (referentiegetal voor
populariteit; leeg = onbekend), tags (kommagescheiden, handmatig gecureerd,
bv. 'stedentrip'; leeg = geen tags). Uit deals_per_jaar volgt de scan-tier
(1 = populairst); zie lib/planner.py voor wat een tier betekent. `tags` wordt
alleen door scripts/build_destination_vibes.py gebruikt (voor het
inschrijfformulier), niet door de scanpijplijn zelf.
"""

import csv
from pathlib import Path

CSV_PATH = Path(__file__).resolve().parent.parent / "destinations.csv"


def tier_for(deals_per_jaar):
    """Tier 1 t/m 4 op basis van het aantal deals per jaar (None = onbekend = tier 4)."""
    if deals_per_jaar is None:
        return 4
    if deals_per_jaar >= 9:
        return 1
    if deals_per_jaar >= 3:
        return 2
    return 3


def load_destinations(path=CSV_PATH):
    """Geeft een lijst dicts: iata, naam, land, deals_per_jaar, tier, tags. Weigert dubbele codes."""
    bestemmingen, gezien = [], set()
    with open(path, newline="", encoding="utf-8") as f:
        for rij in csv.DictReader(f):
            iata = rij["iata"].strip().upper()
            if iata in gezien:
                raise ValueError(f"Dubbele IATA-code in {path}: {iata}")
            gezien.add(iata)
            deals = int(rij["deals_per_jaar"]) if rij["deals_per_jaar"].strip() else None
            tags = [t.strip() for t in rij.get("tags", "").split(",") if t.strip()]
            bestemmingen.append({
                "iata": iata,
                "naam": rij["naam"].strip(),
                "land": rij["land"].strip().upper(),
                "deals_per_jaar": deals,
                "tier": tier_for(deals),
                "tags": tags,
            })
    return bestemmingen
