"""
Poort van website/lib/regions.ts (letterlijk dezelfde landcodes), zodat "intercontinentaal"
hier en op de website hetzelfde betekent. Gebruikt door 1_scanner.py om te bepalen welke
bestemmingen ook Business Class krijgen. tests/test_regions.py bewaakt dat de twee lijsten
niet uit elkaar groeien (zelfde soort bewaking als website/tests/google-flights.test.ts
voor de andere kant van deze poort).
"""

# 'Europa': EU/EER, VK, Zwitserland, Balkan, Turkije, Cyprus, IJsland e.d.
EUROPE = frozenset(
    "AD AL AT BA BE BG BY CH CY CZ DE DK EE ES FI FO FR GB GG GI GR HR HU IE IM IS IT JE LI LT LU LV "
    "MC MD ME MK MT NL NO PL PT RO RS RU SE SI SK SM TR UA VA XK".split()
)


def is_europe(country_code):
    """ISO-3166 alpha-2 (zoals destinations.land). None/leeg telt als 'verder weg'."""
    return bool(country_code) and country_code.upper() in EUROPE
