"""
Supabase-verbinding voor scanner.py (kopie van de private repo's analytics/lib/db.py,
zie de public/private-split in .github/workflows).

Gebruikt de service_role-key (omzeilt RLS) -- alleen bedoeld voor deze
scanner, NOOIT voor de website.
De waarden komen bij een lokale run uit .env in de projectroot (bij een
GitHub Actions-run rechtstreeks uit de workflow-secrets); ze worden nergens
geprint.
"""

import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv(Path(__file__).resolve().parent.parent / ".env")  # projectroot

PAGE_SIZE = 1000  # Supabase geeft standaard maximaal 1000 rijen per request


def _base_url(url: str) -> str:
    """Alleen https://<project>.supabase.co: de client voegt /rest/v1 zelf toe."""
    p = urlparse(url.strip())
    return f"{p.scheme}://{p.netloc}"


def get_client() -> Client:
    return create_client(
        _base_url(os.environ["SUPABASE_URL"]),
        os.environ["SUPABASE_SERVICE_ROLE_KEY"],
    )


def fetch_all(build_query, order_by="id"):
    """
    Haalt ALLE rijen van een query op, pagina voor pagina.

    `build_query` is een functie die telkens een verse query teruggeeft, bijv.
    `lambda: client.table("scans").select("*").eq("is_deal", True)`. De query
    moet een stabiele volgorde hebben; we sorteren op `order_by` (standaard de
    kolom `id`, behalve bij `destinations`, waar `iata` de primary key is).
    """
    rijen, start = [], 0
    while True:
        resp = build_query().order(order_by).range(start, start + PAGE_SIZE - 1).execute()
        rijen.extend(resp.data)
        if len(resp.data) < PAGE_SIZE:
            return rijen
        start += PAGE_SIZE
