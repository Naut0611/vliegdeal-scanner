"""
Gedeelde, pure matchlogica tussen een abonnee-voorkeur (tabel `subscribers`, zie
migrations/008/010_subscriber_*.sql) en een concrete reis (vertrekpunt, bestemmingsland,
vertrekdatum). Oorspronkelijk alleen in send_newsletter.py (welke deals passen bij een
abonnee), hierheen verplaatst zodat 1_scanner.py 'm ook kan gebruiken (welke scancombinaties
sluiten aan bij wat abonnees daadwerkelijk willen) zonder de twee, verder onafhankelijke
scripts van elkaar te laten afhangen.

Geen netwerk/DB: puur rekenwerk op dicts/strings, zie tests/test_send_newsletter.py.
"""


def matches_region(subscriber_region, europe):
    """'alle' | 'europa' | 'verder', zie migrations/008_subscribers.sql."""
    if subscriber_region == "europa":
        return europe
    if subscriber_region == "verder":
        return not europe
    return True  # 'alle'


def matches_origin(subscriber, origin):
    """
    'origin_airports' (meerdere vertrekpunten, migratie 015, aanklikbaar op AirportPicker.tsx) is
    leidend zodra een abonnee die heeft; zonder origin_airports valt dit terug op de oude, enkele
    'origin_airport' -- zelfde val-terug-patroon als matches_countries hieronder t.o.v. region.
    """
    vertrekpunten = subscriber.get("origin_airports")
    if vertrekpunten:
        return origin in vertrekpunten
    return not subscriber.get("origin_airport") or origin == subscriber["origin_airport"]


def matches_countries(subscriber, land, europe):
    """
    'countries' (migratie 010, landcodes zelf gekozen op het inschrijfformulier) is leidend zodra
    een abonnee die heeft; zonder countries (nooit ingevuld, of een abonnee van vóór migratie 010)
    valt dit terug op de oude, grove 'region'-indeling -- zie schema.sql's opmerking bij `subscribers`.
    """
    landen = subscriber.get("countries")
    if landen:
        return land in landen
    return matches_region(subscriber["region"], europe)


def matches_period(subscriber, depart_date):
    """period_from/period_to zijn allebei optioneel (open einde); depart_date is een ISO-datumstring."""
    if subscriber.get("period_from") and depart_date < subscriber["period_from"]:
        return False
    if subscriber.get("period_to") and depart_date > subscriber["period_to"]:
        return False
    return True
