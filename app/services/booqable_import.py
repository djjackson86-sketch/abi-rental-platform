"""Pure helpers for the one-off Booqable -> ABI customer import (2026-09-12).

No database access lives here on purpose: everything is a pure function so it can
be unit-tested against real export rows. The CLI wrapper is
``scripts/import_booqable_customers.py``.

Design rule for addresses: NEVER GUESS. Fields are only filled when a specific,
checkable pattern matches. ``address_line1`` can never end up empty and always
keeps the original text when nothing could be extracted, so no address is ever
lost or silently mangled.
"""
import csv
import json
import re
import unicodedata

SOURCE_SYSTEM = "booqable"

# --------------------------------------------------------------------------- #
# address parsing
# --------------------------------------------------------------------------- #

PROVINCES = {
    "gauteng": "Gauteng", "gp": "Gauteng",
    "kwazulu-natal": "KwaZulu-Natal", "kwazulu natal": "KwaZulu-Natal",
    "kzn": "KwaZulu-Natal", "natal": "KwaZulu-Natal",
    "western cape": "Western Cape", "wc": "Western Cape", "w cape": "Western Cape",
    "eastern cape": "Eastern Cape", "ec": "Eastern Cape", "e cape": "Eastern Cape",
    "free state": "Free State", "fs": "Free State", "vrystaat": "Free State",
    "limpopo": "Limpopo", "lp": "Limpopo",
    "mpumalanga": "Mpumalanga", "mp": "Mpumalanga",
    "north west": "North West", "nw": "North West", "northwest": "North West",
    "northern cape": "Northern Cape", "nc": "Northern Cape",
}

COUNTRIES = {"south africa", "rsa", "sa", "s a", "zuid afrika"}

CITIES = sorted({
    "johannesburg", "joburg", "sandton", "randburg", "roodepoort", "soweto", "midrand",
    "centurion", "pretoria", "tshwane", "benoni", "boksburg", "germiston", "kempton park",
    "edenvale", "alberton", "springs", "brakpan", "nigel", "heidelberg", "vereeniging",
    "vanderbijlpark", "sasolburg", "krugersdorp", "ruimsig", "muldersdrift", "fourways",
    "bryanston", "rosebank", "sunninghill", "woodmead", "olifantsfontein", "carletonville",
    "cape town", "capetown", "kaapstad", "bellville", "paarl", "stellenbosch",
    "somerset west", "george", "knysna", "mossel bay", "oudtshoorn", "worcester",
    "hermanus", "malmesbury", "durban", "pinetown", "umhlanga", "pietermaritzburg",
    "ballito", "richards bay", "newcastle", "port shepstone", "empangeni", "vryheid",
    "port elizabeth", "gqeberha", "east london", "uitenhage", "queenstown", "mthatha",
    "bloemfontein", "welkom", "bethlehem", "kroonstad", "polokwane", "pietersburg",
    "tzaneen", "thohoyandou", "lephalale", "musina", "nelspruit", "mbombela",
    "witbank", "emalahleni", "middelburg", "ermelo", "secunda", "rustenburg",
    "potchefstroom", "klerksdorp", "mahikeng", "mafikeng", "brits", "kimberley",
    "upington", "springbok", "kuruman",
}, key=len, reverse=True)          # longest first so "cape town" beats "town"

POBOX = re.compile(r"\b(p\.?\s?o\.?\s?box|postnet|private bag)\b", re.I)
ONLY4 = re.compile(r"^(\d{4})$")
LEAD4 = re.compile(r"^(\d{4})\s+(.+)$")


def norm(value):
    """Normalise unicode oddities and whitespace without changing meaning."""
    if not value:
        return ""
    v = unicodedata.normalize("NFKC", value)
    v = "".join(ch for ch in v if unicodedata.category(ch) != "Cf")   # bidi marks etc.
    v = v.replace("\xa0", " ")
    v = re.sub(r"\s+", " ", v)
    v = re.sub(r"\s*,\s*", ", ", v)
    return v.strip(" ,")


def _city_in(segment):
    low = " " + segment.lower() + " "
    for city in CITIES:
        if re.search(r"[^a-z]" + re.escape(city) + r"[^a-z]", low):
            return city
    return ""


def _strip_trailing_city(segment, city):
    return re.sub(r"[\s,]*" + re.escape(city) + r"\s*$", "", segment, flags=re.I).strip(" ,-")


def split_address(raw):
    """Split a Booqable free-text address into our structured fields.

    Returns a dict with address_line1, address_line2, suburb, city, province,
    postal_code and confidence ('full' | 'partial' | 'fallback').
    """
    out = {"address_line1": "", "address_line2": "", "suburb": "", "city": "",
           "province": "", "postal_code": "", "confidence": "fallback"}
    text = norm(raw)
    if not text:
        return out
    original = text
    segs = [s.strip() for s in text.split(",") if s.strip()]
    comma_form = len(segs) > 1

    # country
    if segs and segs[-1].lower().strip(".") in COUNTRIES:
        segs.pop()

    # province: only an exact province name/abbreviation standing alone
    province = ""
    kept = []
    for seg in segs:
        key = re.sub(r"[^a-z ]", "", seg.lower()).strip()
        if key in PROVINCES and not province:
            province = PROVINCES[key]
        else:
            kept.append(seg)
    segs = kept

    # postal code: comma form, last remaining segment exactly four digits
    postal = ""
    if comma_form and len(segs) >= 2:
        match = ONLY4.match(segs[-1])
        if match:
            postal = match.group(1)
            segs.pop()
        else:
            lead = LEAD4.match(segs[-1])          # e.g. "1685 Midrand"
            if lead and _city_in(lead.group(2)):
                postal, segs[-1] = lead.group(1), lead.group(2).strip()

    # city: whole-segment match first, then a tail match
    city = ""
    city_idx = -1
    for i in range(len(segs) - 1, -1, -1):
        clean = re.sub(r"[^a-z ]", "", segs[i].lower()).strip()
        if clean in CITIES:
            city, city_idx = clean.title(), i
            break
    if not city:
        for i in range(len(segs) - 1, -1, -1):
            found = _city_in(segs[i])
            if found:
                city, city_idx = found.title(), i
                segs[i] = _strip_trailing_city(segs[i], found)
                if not segs[i]:
                    segs.pop(i)
                    city_idx = -1
                break

    # suburb: digit-free 1-3 word segment directly before the city
    suburb = ""
    if city_idx > 0:
        cand = segs[city_idx - 1]
        if (not re.search(r"\d", cand) and 1 <= len(cand.split()) <= 3
                and 2 < len(cand) < 40 and not POBOX.search(cand)):
            if re.sub(r"[^a-z ]", "", cand.lower()).strip() not in CITIES:
                suburb = cand
                segs.pop(city_idx - 1)
                city_idx -= 1

    # a whole-segment city belongs in `city`, not repeated in line 1
    if city and 0 <= city_idx < len(segs) and segs[city_idx].strip().lower() == city.lower():
        if len(segs) > 1:
            segs.pop(city_idx)

    line1 = ", ".join(s for s in segs if s).strip(" ,-")
    if not line1:
        line1 = original                       # never lose the address

    out.update(address_line1=line1, city=city, province=province,
               postal_code=postal, suburb=suburb)

    if city or province or postal:
        out["confidence"] = "full" if (city and (province or postal)) else "partial"
    else:
        out["confidence"] = "fallback"
        out["address_line1"] = original
        out["city"] = out["suburb"] = ""
    return out


# --------------------------------------------------------------------------- #
# customer mapping
# --------------------------------------------------------------------------- #

COMPANY_HINT = re.compile(
    r"\b(pty|ltd|inc|cc|company|companies|corporation|holding|holdings|logistics|"
    r"transport|trading|enterprises|investments|group|service|services|solutions|"
    r"construction|plant hire|hire|rental|rentals|mining|supplies|suppliers|"
    r"manufacturing|projects|developments|properties|delivery|distributors|"
    r"wholesalers|retailers|tours|travel|security|consulting|consultants|engineering|"
    r"farming|brothers|associates|agencies|agency|academy|school|college|church|trust|"
    r"foundation|contractors|industrial|enterprise|ventures|partners|carriers|couriers|"
    r"freight|motors|hardware|builders|civils|borehole|stationers|equipment|"
    r"refrigeration|electrical|plumbing|roofing|glass|tyres|spares)\b", re.I)

# Kept in custom_fields_json but hidden from every UI surface (see
# HIDDEN_CUSTOM_FIELD_KEYS in app/services/customers.py).
AUDIT_KEYS = (
    "booqable_id", "booqable_number", "booqable_balance_due_cents",
    "booqable_deposit_type", "booqable_deposit_value", "booqable_client_verification",
    "booqable_latest_order_at", "booqable_your_reference", "booqable_tags",
    "booqable_updated_at", "booqable_address_raw",
)


def infer_customer_type(name, vat_no="", company_reg_no=""):
    """Company when there is registration evidence or an unmistakable business name."""
    if (vat_no or "").strip() or (company_reg_no or "").strip():
        return "company"
    if COMPANY_HINT.search(name or ""):
        return "company"
    return "individual"


def to_local_datetime(value):
    """'2022-12-12 15:42:28 +0200' -> '2022-12-12T15:42:28' (already SAST)."""
    text = norm(value)
    if not text:
        return ""
    match = re.match(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)", text)
    if not match:
        return text
    time_part = match.group(2)
    if len(time_part) == 5:
        time_part += ":00"
    return f"{match.group(1)}T{time_part}"


def _as_float(value, default=0.0):
    try:
        return float(str(value).strip() or default)
    except (TypeError, ValueError):
        return default


def build_custom_fields(row, address):
    """Map the Booqable columns we have no first-class column for."""
    fields = {
        "vehicle_make": norm(row.get("vehicle_make")),
        "vehicle_color": norm(row.get("vehicle_colour")),          # UK -> US key
        "vehicle_reg_no": norm(row.get("vehicle_registration")).upper(),
        "alternative_contact_name": norm(row.get("alternative_contact_person")),
        "alternative_contact_number": norm(row.get("alternative_contact_number")),
        "vat_number": norm(row.get("customer_vat_no")),
        "company_reg_no": norm(row.get("company_reg_no")),
    }
    audit = {
        "booqable_id": norm(row.get("id")),
        "booqable_number": norm(row.get("number")),
        "booqable_balance_due_cents": norm(row.get("balance_due_in_cents")),
        "booqable_deposit_type": norm(row.get("deposit_type")),
        "booqable_deposit_value": norm(row.get("deposit_value")),
        "booqable_client_verification": norm(row.get("client_verification")),
        "booqable_latest_order_at": norm(row.get("latest_order_at")),
        "booqable_your_reference": norm(row.get("your_reference")),
        "booqable_tags": norm(row.get("tags")),
        "booqable_updated_at": norm(row.get("updated_at")),
        # keep the raw address so a failed split can always be revisited
        "booqable_address_raw": norm(row.get("main")) if address.get("confidence") != "fallback" else "",
    }
    fields.update(audit)
    return {k: v for k, v in fields.items() if v}


def map_customer_row(row):
    """CSV row -> dict of customers-column values (no id, no created_by_user_id)."""
    address = split_address(row.get("main"))
    name = norm(row.get("name"))
    custom = build_custom_fields(row, address)
    consented = str(row.get("email_marketing_consented", "")).strip().lower()
    payload = {
        "customer_type": infer_customer_type(name, row.get("customer_vat_no"), row.get("company_reg_no")),
        "name": name,
        "email": norm(row.get("email")),
        "phone": norm(row.get("phone")),
        "marketing_opt_in": 1 if consented == "true" else 0,
        "address_line1": address["address_line1"],
        "address_line2": address["address_line2"],
        "suburb": address["suburb"],
        "city": address["city"],
        "province": address["province"],
        "postal_code": address["postal_code"],
        "country": "South Africa",
        "custom_fields_json": json.dumps(custom, ensure_ascii=False),
        "standard_discount_percent": _as_float(row.get("discount_percentage")),
        "created_at": to_local_datetime(row.get("created_at")),
        "source_system": SOURCE_SYSTEM,
        "source_id": norm(row.get("id")),
    }
    payload["_address"] = address
    payload["_custom"] = custom
    return payload


def read_export(path):
    """Read a Booqable customer export (UTF-8, BOM tolerated)."""
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def validate(payload):
    """Return a list of blocking problems for one mapped row."""
    problems = []
    if not payload["name"]:
        problems.append("missing name")
    if not payload["source_id"]:
        problems.append("missing Booqable id")
    if not payload["created_at"]:
        problems.append("missing created_at")
    return problems
