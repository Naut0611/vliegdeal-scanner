"""
Gedeelde kwaliteitsgrens voor deals. Staat in lib/ (niet in 2_curate.py) omdat meerdere stappen
'm nodig hebben en bestandsnamen met een cijfer niet te importeren zijn.
"""

# Harde ondergrens op de ECHTE korting (discount_pct of drop_pct, het hoogste van de twee), los
# van FLOOR_BONUS/RARITY_BONUS. Gebruikt door 2_curate.py (kandidaten), 4_review.py --automatisch
# (publicatiepoort) en unpublish_onder_drempel.py.
MIN_DISCOUNT_PCT = 40.0


def echte_korting(deal):
    return max(deal.get("discount_pct") or 0, deal.get("drop_pct") or 0)
