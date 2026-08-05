"""Normalization primitives.

Deduplication is only as good as normalization. "Joe's Plumbing, Inc." and
"JOE'S PLUMBING INC" and "Joes Plumbing" must collapse to the same token before
any hashing happens, or the database ends up with three rows for one shop and
you call the owner three times.

Everything here is pure and synchronous so it can be unit-tested exhaustively —
see ``tests/test_normalize.py``.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import urlparse, urlunparse

import phonenumbers
import tldextract

# tldextract normally phones home to refresh the public-suffix list. In a
# nightly batch job that is an unwanted network dependency and a startup
# stall, so we pin it to the bundled snapshot.
_extract = tldextract.TLDExtract(suffix_list_urls=())

# Legal-entity suffixes and filler that carry no identifying information.
_NAME_NOISE = re.compile(
    r"\b(inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|"
    r"pllc|plc|pc|lp|llp|dba|the|and|of|group|holdings|enterprises|services|"
    r"service|solutions)\b",
    re.IGNORECASE,
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_WHITESPACE = re.compile(r"\s+")

# USPS street-suffix normalization. Keys are what people type; values are the
# canonical form. Only the suffixes that actually collide in practice.
_STREET_ABBREV = {
    "street": "st", "st.": "st",
    "avenue": "ave", "av": "ave", "ave.": "ave",
    "boulevard": "blvd", "blvd.": "blvd",
    "road": "rd", "rd.": "rd",
    "drive": "dr", "dr.": "dr",
    "lane": "ln", "ln.": "ln",
    "court": "ct", "ct.": "ct",
    "circle": "cir", "cir.": "cir",
    "place": "pl", "pl.": "pl",
    "parkway": "pkwy", "pkwy.": "pkwy",
    "highway": "hwy", "hwy.": "hwy",
    "suite": "ste", "ste.": "ste", "#": "ste",
    "apartment": "apt", "apt.": "apt",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
}

GENERIC_EMAIL_LOCALS = {
    "info", "contact", "hello", "admin", "office", "sales", "support",
    "help", "team", "mail", "inquiries", "inquiry", "service", "customerservice",
    "frontdesk", "reception", "general", "webmaster", "noreply", "no-reply",
}

# Addresses/domains that are never a real business contact.
EMAIL_BLOCKLIST_DOMAINS = {
    "example.com", "sentry.io", "wixpress.com", "godaddy.com", "squarespace.com",
    "wordpress.com", "shopify.com", "domain.com", "email.com", "yourdomain.com",
    "sentry-next.wixpress.com",
}

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def strip_accents(value: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch)
    )


def normalize_name(name: str | None) -> str:
    """Collapse a business name to a comparable token.

    ``"Joe's Plumbing, Inc."`` -> ``"joesplumbing"``
    """
    if not name:
        return ""
    value = strip_accents(name).lower()
    value = value.replace("&", " and ")
    value = _NAME_NOISE.sub(" ", value)
    value = _NON_ALNUM.sub("", value)
    return value


def display_name_tokens(name: str | None) -> set[str]:
    """Word set used for fuzzy comparison (Jaccard) rather than exact match."""
    if not name:
        return set()
    value = strip_accents(name).lower()
    value = _NAME_NOISE.sub(" ", value)
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    return {t for t in value.split() if len(t) > 1}


def normalize_street(address: str | None) -> str:
    """Canonicalize a street line for hashing.

    ``"123 North Main Street, Suite 4"`` -> ``"123nmainstste4"``
    """
    if not address:
        return ""
    value = strip_accents(address).lower()
    value = value.replace(",", " ").replace(".", " ")
    value = _WHITESPACE.sub(" ", value).strip()
    parts = [_STREET_ABBREV.get(tok, tok) for tok in value.split()]
    return _NON_ALNUM.sub("", "".join(parts))


def normalize_phone(phone: str | None, region: str = "US") -> str | None:
    """Return E.164 (``+16269407551``) or ``None`` if unparseable.

    E.164 is the only phone representation worth storing: it is unambiguous,
    fixed-format, and directly comparable, which is exactly what dedupe needs.
    """
    if not phone:
        return None
    try:
        parsed = phonenumbers.parse(phone, region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def format_phone_display(phone_e164: str | None) -> str | None:
    """``+16269407551`` -> ``(626) 940-7551``"""
    if not phone_e164:
        return None
    try:
        parsed = phonenumbers.parse(phone_e164, "US")
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.NATIONAL)
    except phonenumbers.NumberParseException:
        return phone_e164


def normalize_url(url: str | None) -> str | None:
    """Add a scheme if missing, drop fragments and tracking noise."""
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url.lstrip("/")
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if not parsed.netloc:
        return None
    netloc = parsed.netloc.lower().rstrip(".")
    path = parsed.path or "/"
    return urlunparse((parsed.scheme.lower(), netloc, path, "", parsed.query, ""))


def extract_domain(url: str | None) -> str | None:
    """Registrable domain, without ``www.``  (``https://www.a.co.uk/x`` -> ``a.co.uk``)."""
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url
    result = _extract(url)
    if not result.domain or not result.suffix:
        return None
    return f"{result.domain}.{result.suffix}".lower()


def root_host(url: str | None) -> str | None:
    """Full hostname including subdomain, lowercased, ``www.`` stripped."""
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return None
    host = host.split(":")[0]
    return host[4:] if host.startswith("www.") else host or None


def normalize_zip(zip_code: str | None) -> str | None:
    """Keep ZIP5; ZIP+4 defeats matching because providers disagree on the +4."""
    if not zip_code:
        return None
    digits = re.sub(r"\D", "", zip_code)
    return digits[:5] if len(digits) >= 5 else None


def normalize_state(state: str | None) -> str | None:
    if not state:
        return None
    state = state.strip()
    if len(state) == 2:
        return state.upper()
    return _STATE_NAMES.get(state.lower())


def compute_dedupe_key(
    name: str | None,
    street: str | None,
    zip_code: str | None,
    *,
    phone_e164: str | None = None,
) -> str:
    """Stable identity hash for a business.

    Primary basis is name + street + ZIP. When a record has no street address
    (common for mobile/service-area businesses), phone number substitutes —
    without it, every mobile detailer in a city would collapse into one row.
    """
    n = normalize_name(name)
    s = normalize_street(street)
    z = normalize_zip(zip_code) or ""
    basis = f"{n}|{s}|{z}" if s else f"{n}|phone:{phone_e164 or ''}|{z}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def extract_emails(text: str) -> list[str]:
    """Pull plausible business emails out of page text.

    Filters image filenames that look like addresses, tracking pixels, and the
    platform noise (``...@sentry.wixpress.com``) that would otherwise dominate
    results on hosted-builder sites.
    """
    found: list[str] = []
    seen: set[str] = set()
    for match in _EMAIL_RE.findall(text or ""):
        email = match.lower().strip(".")
        if email in seen:
            continue
        local, _, domain = email.partition("@")
        if domain in EMAIL_BLOCKLIST_DOMAINS:
            continue
        if any(domain.endswith(f".{d}") for d in ("wixpress.com", "sentry.io")):
            continue
        if re.search(r"\.(png|jpe?g|gif|svg|webp|css|js)$", email):
            continue
        if len(local) > 64 or len(email) > 254:
            continue
        # Hex blobs are almost always asset hashes, not people.
        if re.fullmatch(r"[0-9a-f]{16,}", local):
            continue
        seen.add(email)
        found.append(email)
    return found


def is_generic_email(email: str) -> bool:
    local = email.split("@", 1)[0].lower()
    return local in GENERIC_EMAIL_LOCALS


def guess_owner_name(email: str) -> str | None:
    """Best-effort person name from a personal-looking address.

    ``john.smith@acme.com`` -> ``"John Smith"``. Returns ``None`` for role
    addresses, so we never put "Info" in the owner-name column.
    """
    local = email.split("@", 1)[0]
    if is_generic_email(email) or any(ch.isdigit() for ch in local):
        return None
    parts = [p for p in re.split(r"[._\-]+", local) if p.isalpha() and len(p) > 1]
    if not 1 <= len(parts) <= 3:
        return None
    return " ".join(p.capitalize() for p in parts)


_STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "puerto rico": "PR",
}

US_STATES = sorted(set(_STATE_NAMES.values()))
