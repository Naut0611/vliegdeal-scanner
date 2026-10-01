"""
Scan-rooster: welke (route, datum)-combinaties zijn vandaag aan de beurt?

Met ~400 bestemmingen x 3 vertrekpunten x 6-12 datumparen is een volledige ronde
veel te groot voor één run. In plaats daarvan krijgt elke bestemming een tier
(populariteit), elke tier een doelinterval, en kiest `select_due_combos` per run
de meest achterstallige combinaties, tot het budget op is.

  prioriteit = leeftijd van de laatste scan / doelinterval van de tier
  - alleen combinaties met prioriteit >= 1 komen in aanmerking
  - nooit-gescande combinaties tellen als prioriteit NEVER_SCANNED_RATIO, met
    tier 1 eerst en binnen een tier willekeurig
  - de gekozen combinaties worden geschud (spreiding over routes/vertrekpunten)

Alle functies zijn puur (geen netwerk/DB), zodat ze goed te testen zijn.
"""

import random
from datetime import datetime
from typing import NamedTuple

# samples_per_month: hoeveel datumparen per maand (x MONTHS_AHEAD maanden) per route.
# interval_days: hoe vaak we een combinatie ideaal opnieuw scannen.
TIER_CONFIG = {
    1: {"samples_per_month": 2, "interval_days": 6},    # >= 9 deals/jaar
    2: {"samples_per_month": 1, "interval_days": 10},   # 3-8 deals/jaar
    3: {"samples_per_month": 1, "interval_days": 28},   # 1-2 deals/jaar
    4: {"samples_per_month": 1, "interval_days": 42},   # geen gegevens
}

# Een nooit-gescande combinatie is even urgent als een combinatie die 2x zo lang
# als zijn interval niet is gescand: nieuwe routes krijgen snel een eerste meting,
# maar de vernieuwing van populaire routes blijft niet maandenlang liggen.
NEVER_SCANNED_RATIO = 2.0

# Basis-verblijfsduurgrenzen (min_nachten, max_nachten) per regio, voor het RETOURraster
# (lib/scraper.fetch_return_date_prices/filter_by_stay_length): een verre reis vraagt sowieso meer
# dagen om de vliegtijd de moeite waard te maken, dus een hogere ondergrens EN een hogere bovengrens
# dan een Europese vakantie.
BASE_STAY_BOUNDS = {
    "europa": (3, 21),
    "verre_reis": (10, 30),
}


def stay_bounds(tags, europa):
    """
    (min_nachten, max_nachten, anker_dagen) voor het retourraster. De regio (europa) bepaalt de
    basisgrenzen (zie BASE_STAY_BOUNDS); de 'stedentrip'-tag (destinations.csv, handmatig gecureerd)
    verlaagt daarbinnen ALLEEN de ondergrens naar 2 nachten -- een stedentrip mag kort, maar sluit
    een langere vakantie naar diezelfde bestemming niet uit (San Francisco is bijv. zowel een
    stedentrip van 5-7 dagen als een langere vakantie mogelijk, dus de bovengrens blijft gewoon die
    van zijn regio i.p.v. een eigen, krappe stedentrip-bovengrens). De anker is het midden van het
    resulterende bereik, zodat Google's venster (dat breder wordt naarmate de anker groter is, zie
    CLAUDE.md) zo goed mogelijk de hele gewenste spreiding dekt.
    """
    min_nachten, max_nachten = BASE_STAY_BOUNDS["europa" if europa else "verre_reis"]
    if "stedentrip" in tags:
        min_nachten = 2
    anker_dagen = round((min_nachten + max_nachten) / 2)
    return min_nachten, max_nachten, anker_dagen


class Combo(NamedTuple):
    origin: str
    destination: str
    depart_date: str
    return_date: str
    tier: int
    cabin_class: str = "economy"
    # Retourraster-grenzen voor deze combinatie (zie stay_bounds() hierboven); de standaardwaarden
    # komen overeen met de 'europa'-categorie, dus ongewijzigd gedrag voor elke Combo die (zoals in
    # tests of OLD_1_scanner.py) buiten build_combos() om wordt aangemaakt.
    min_stay_days: int = 3
    max_stay_days: int = 21
    retour_anker_days: int = 12

    @property
    def key(self):
        # cabin_class erbij: Economy- en Business-scanrecentheid worden onafhankelijk
        # bijgehouden, anders telt een Economy-scan van vandaag ten onrechte als
        # 'recent gescand' voor Business (en andersom).
        return (self.origin, self.destination, self.depart_date, self.return_date, self.cabin_class)


def steady_state_load(combos):
    """Gemiddeld aantal scans per dag als elke combinatie precies op zijn interval wordt vernieuwd."""
    return sum(1 / TIER_CONFIG[c.tier]["interval_days"] for c in combos)


def priority_ratio(combo, last_scanned, now):
    """Leeftijd / interval; NEVER_SCANNED_RATIO als er nog nooit is gescand."""
    laatste = last_scanned.get(combo.key)
    if laatste is None:
        return NEVER_SCANNED_RATIO
    leeftijd_dagen = (now - laatste).total_seconds() / 86400
    return leeftijd_dagen / TIER_CONFIG[combo.tier]["interval_days"]


def select_due_combos(combos, last_scanned, now, budget, rng=random, prioriteit_fn=None):
    """
    Kiest hoogstens `budget` combinaties die aan de beurt zijn.

    combos        lijst Combo
    last_scanned  {combo.key: datetime van de laatste scan (ook mislukte)}
    now           datetime (zelfde tijdzone-soort als last_scanned)
    prioriteit_fn optioneel: Combo -> getal, laag = eerst. Weegt zwaarder dan recency: bij een
                  beperkt budget raakt een hogere-prioriteitsgroep (lager getal) dus EERST
                  volledig bijgewerkt voordat een lagere-prioriteitsgroep aan de beurt komt,
                  ongeacht hoe lang die laatste al niet gescand is. None (standaard): alle
                  combinaties gelijk, ongewijzigd t.o.v. voorheen. Bewust een functie i.p.v.
                  bijv. een origin->getal-dict: een prioriteit kan op willekeurige velden van de
                  Combo berusten (bv. 1_scanner.py's abonnee-interesse, die op origin ÉN
                  destination ÉN datum let, niet op één veld alleen).
    Geeft (gekozen, aantal_aan_de_beurt) terug; `gekozen` is geschud.
    """
    prioriteit_fn = prioriteit_fn or (lambda c: 0)
    aan_de_beurt = []
    for c in combos:
        ratio = priority_ratio(c, last_scanned, now)
        if ratio >= 1:
            aan_de_beurt.append((prioriteit_fn(c), -ratio, c.tier, rng.random(), c))

    aan_de_beurt.sort(key=lambda t: t[:4])
    gekozen = [c for *_, c in aan_de_beurt[:max(budget, 0)]]
    rng.shuffle(gekozen)
    return gekozen, len(aan_de_beurt)
