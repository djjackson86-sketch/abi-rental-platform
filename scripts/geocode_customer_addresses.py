#!/usr/bin/env python3
"""Geocode the imported customer addresses into structured fields (Nominatim/OSM).

Read-only against the app; writes a CACHE and a PROPOSAL file only. Nothing is
written to the database - `scripts/apply_geocoded_addresses.py` does that after review.

Why multi-pass: a single free-form Nominatim query resolves clean street addresses
well but fails on South African township/informal lines ("Diepsloot Ext 5 Makhulong
Street House No 4234"). So each address is tried in decreasing specificity and the
best result is tagged with its precision:

    address  - street level (house number / road known)
    suburb   - suburb or neighbourhood known
    area     - only city/town/county known
    none     - nothing usable

Resumable: every query/response pair is appended to a JSONL cache, so a restart
(or a re-run after a crash/suspension) never re-queries the same string.

Nominatim policy: max 1 request/second, identifying User-Agent required.
"""
import argparse
import csv
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.expanduser("~/abi-backups/booqable-import")
CACHE = os.path.join(BASE, "geocode-cache.jsonl")
PROPOSALS = os.path.join(BASE, "address-proposals.csv")
ENDPOINT = "https://nominatim.openstreetmap.org/search"
UA = {
    "User-Agent": "SanoTrailers-AddressCleanup/1.0 (contact: info@sanotrailers.co.za)",
    "Accept-Language": "en-ZA,en",
}
THROTTLE = 1.15           # seconds between requests (policy: max 1/sec)
COUNTRY = "South Africa"

CITY_HINTS = [
    "midrand", "johannesburg", "joburg", "sandton", "randburg", "roodepoort", "soweto",
    "pretoria", "centurion", "tshwane", "benoni", "boksburg", "germiston", "kempton park",
    "edenvale", "alberton", "springs", "brakpan", "nigel", "heidelberg", "vereeniging",
    "vanderbijlpark", "sasolburg", "krugersdorp", "fourways", "bryanston", "rosebank",
    "cape town", "capetown", "bellville", "paarl", "stellenbosch", "somerset west",
    "george", "knysna", "mossel bay", "oudtshoorn", "worcester", "hermanus", "malmesbury",
    "durban", "pinetown", "umhlanga", "pietermaritzburg", "ballito", "richards bay",
    "newcastle", "port shepstone", "empangeni", "vryheid", "port elizabeth", "gqeberha",
    "east london", "uitenhage", "queenstown", "mthatha", "bloemfontein", "welkom",
    "bethlehem", "kroonstad", "polokwane", "pietersburg", "tzaneen", "thohoyandou",
    "lephalale", "musina", "nelspruit", "mbombela", "witbank", "emalahleni", "middelburg",
    "ermelo", "secunda", "rustenburg", "potchefstroom", "klerksdorp", "mahikeng",
    "mafikeng", "brits", "kimberley", "upington", "springbok", "kuruman", "olifantsfontein",
]
INFORMAL = re.compile(r"\b(ext\.?|extension|house\s*no|house\s*number|stand\s*no|stand\s*number|"
                      r"site|plot|erf|no\.?|nr\.?)\b", re.I)


STREET_WORDS = {
    "street", "st", "road", "rd", "avenue", "ave", "drive", "dr", "close", "crescent",
    "cres", "lane", "way", "place", "pl", "court", "ct", "boulevard", "blvd", "highway",
    "circle", "square", "walk", "park", "estate", "gardens", "view", "ridge", "meadows",
}
INFORMAL_WORDS = {
    "ext", "extension", "house", "home", "stand", "plot", "erf", "site", "block", "unit",
    "flat", "room", "no", "nr", "number", "the", "and", "south", "africa", "rsa", "sa",
    "complex", "village", "township", "section", "phase", "street",
}


def _place_word(word):
    low = word.lower()
    if low in STREET_WORDS or low in INFORMAL_WORDS:
        return False
    if re.fullmatch(r"[a-z]*\d+[a-z]*", low):        # ext4, block9, u4960
        return False
    return len(low) >= 4


GENERIC_WORDS = {
    "city", "town", "park", "view", "ridge", "gardens", "estate", "north", "south",
    "east", "west", "central", "upper", "lower", "new", "old", "main", "plot",
}


def place_candidates(text):
    """Place-like names to try on their own, best guess first.

    South African addresses put the area near the end, so later segments win; and
    two-word areas ("Birch Acres", "Cosmo City") must stay joined - but only when
    the words were actually next to each other in the source.
    """
    parts = [p.strip() for p in (text or "").split(",") if p.strip()]
    out = []
    for part in reversed(parts):
        if re.fullmatch(r"\d{4,5}", part) or part.lower().strip(".") in {"south africa", "rsa", "sa"}:
            continue
        words = re.findall(r"[A-Za-z][A-Za-z'\-]+", part)
        kept = [i for i, w in enumerate(words) if _place_word(w)]
        # adjacent pairs only, e.g. "Cosmo City" but never "Diepsloot Makhulong"
        for pos, idx in enumerate(kept):
            if pos + 1 < len(kept) and kept[pos + 1] == idx + 1:
                pair = f"{words[idx]} {words[idx + 1]}"
                if len(pair) <= 26:
                    out.append(pair)
        for idx in reversed(kept):
            word = words[idx]
            if word.lower() in GENERIC_WORDS:
                continue
            out.append(word)
    seen, result = set(), []
    for name in out:
        if name.lower() not in seen:
            seen.add(name.lower())
            result.append(name)
    return result[:3]


def load_cache():
    cache = {}
    if os.path.exists(CACHE):
        with open(CACHE) as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    cache[entry["q"]] = entry
                except Exception:
                    continue
    return cache


def query(q, cache, cache_fh, pause):
    """One Nominatim search, cached on disk."""
    if q in cache:
        return cache[q].get("result")
    url = ENDPOINT + "?" + urllib.parse.urlencode({
        "format": "jsonv2", "q": q, "countrycodes": "za",
        "addressdetails": "1", "limit": "1",
    })
    result = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers=UA)
            payload = json.loads(urllib.request.urlopen(req, timeout=45).read())
            result = payload[0] if payload else None
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503):
                time.sleep(5 + attempt * 5)
                continue
            break
        except Exception:
            time.sleep(2 + attempt * 2)
            continue
    entry = {"q": q, "result": result, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    cache[q] = entry
    cache_fh.write(json.dumps(entry) + "\n")
    cache_fh.flush()
    time.sleep(pause)
    return result


def build_queries(raw):
    """Decreasing-specificity query ladder for one raw address."""
    text = re.sub(r"\s+", " ", (raw or "")).strip(" ,")
    if not text:
        return []
    base = text if text.lower().endswith("south africa") else f"{text}, {COUNTRY}"
    queries = [base]
    parts = [p.strip() for p in text.split(",") if p.strip()]
    # drop informal tokens and house numbers, keep street + area
    cleaned = INFORMAL.sub(" ", text)
    cleaned = re.sub(r"^\s*\d+[A-Za-z]?\s+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,")
    if cleaned and cleaned.lower() != text.lower():
        queries.append(f"{cleaned}, {COUNTRY}")
    # last two comma parts (suburb/city) - but never build a query from a bare
    # postal code or a country name: "1686, South Africa" is dangerously ambiguous
    # and matched Gauteng addresses to Limpopo.
    useful = [p for p in parts if not re.fullmatch(r"\s*\d{4}\s*", p)
              and p.lower().strip(".") not in {"south africa", "rsa", "sa"}]
    if len(useful) >= 2:
        queries.append(", ".join(useful[-2:]) + f", {COUNTRY}")
    elif len(useful) == 1 and useful[0] not in parts[:1]:
        queries.append(f"{useful[0]}, {COUNTRY}")
    # any known city mentioned in the string
    low = text.lower()
    for city in CITY_HINTS:
        if re.search(r"\b" + re.escape(city) + r"\b", low):
            queries.append(f"{city.title()}, {COUNTRY}")
            break
    # dedupe, keep order
    seen, out = set(), []
    for q in queries:
        if q.lower() not in seen:
            seen.add(q.lower())
            out.append({"q": q, "fallback": False})
    # last resort: try the place names on their own. These can only ever yield an
    # area-level answer, so they run last and the FIRST hit wins (a later, vaguer
    # candidate must not overwrite a good earlier one).
    for name in place_candidates(text):
        candidate = f"{name}, {COUNTRY}"
        if candidate.lower() not in seen:
            seen.add(candidate.lower())
            out.append({"q": candidate, "fallback": True})
    return out


def pick(result):
    """Map a Nominatim hit to our fields + a precision tag."""
    if not result:
        return None
    a = result.get("address", {}) or {}
    road = a.get("road") or a.get("pedestrian") or a.get("footway") or ""
    house = a.get("house_number") or ""
    suburb = a.get("suburb") or a.get("neighbourhood") or a.get("quarter") or a.get("hamlet") or ""
    city = (a.get("city") or a.get("town") or a.get("village") or a.get("municipality")
            or a.get("county") or a.get("state_district") or "")
    state = a.get("state") or ""
    postcode = a.get("postcode") or ""
    line1 = " ".join(p for p in (house, road) if p).strip()
    if house and road:
        precision = "address"
    elif road:
        precision = "street"
    elif suburb:
        precision = "suburb"
    elif city:
        precision = "area"
    else:
        precision = "none"
    return {
        "address_line1": line1, "suburb": suburb, "city": city,
        "province": state, "postal_code": postcode,
        "lat": result.get("lat", ""), "lon": result.get("lon", ""),
        "display_name": result.get("display_name", ""), "precision": precision,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default=os.path.join(BASE, "customers-export-2026-09-12.csv"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--pause", type=float, default=THROTTLE)
    parser.add_argument("--out", default=PROPOSALS)
    args = parser.parse_args()

    customers = list(csv.DictReader(open(args.csv, encoding="utf-8-sig")))
    targets = [c for c in customers if (c.get("main") or "").strip()]
    if args.limit:
        targets = targets[:args.limit]
    print(f"customers with an address: {len(targets)}", flush=True)

    cache = load_cache()
    print(f"cache already holds {len(cache)} queries", flush=True)

    done = set()
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                done.add(row["booqable_id"])
    print(f"already proposed: {len(done)}", flush=True)

    fields = ["booqable_id", "booqable_number", "name", "raw_address",
              "address_line1", "suburb", "city", "province", "postal_code",
              "lat", "lon", "precision", "matched_query", "display_name"]
    new_file = not os.path.exists(args.out)
    out_fh = open(args.out, "a", newline="", encoding="utf-8-sig")
    writer = csv.DictWriter(out_fh, fieldnames=fields)
    if new_file:
        writer.writeheader()

    cache_fh = open(CACHE, "a")
    processed = 0
    try:
        for customer in targets:
            if customer["id"] in done:
                continue
            raw = customer.get("main") or ""
            best = None
            used_query = ""
            for step in build_queries(raw):
                q = step["q"]
                result = query(q, cache, cache_fh, args.pause)
                candidate = pick(result)
                if candidate and candidate["precision"] != "none":
                    best, used_query = candidate, q
                    if candidate["precision"] in ("address", "street"):
                        break        # good enough, stop laddering
                    if step["fallback"]:
                        break        # first bare-place hit wins
            writer.writerow({
                "booqable_id": customer["id"],
                "booqable_number": customer.get("number", ""),
                "name": customer.get("name", "").strip(),
                "raw_address": raw,
                "address_line1": best["address_line1"] if best else "",
                "suburb": best["suburb"] if best else "",
                "city": best["city"] if best else "",
                "province": best["province"] if best else "",
                "postal_code": best["postal_code"] if best else "",
                "lat": best["lat"] if best else "",
                "lon": best["lon"] if best else "",
                "precision": best["precision"] if best else "none",
                "matched_query": used_query,
                "display_name": best["display_name"] if best else "",
            })
            out_fh.flush()
            processed += 1
            if processed % 25 == 0:
                print(f"  {processed} processed (cache {len(cache)})", flush=True)
    finally:
        out_fh.close()
        cache_fh.close()
    print(f"DONE: {processed} new proposals written to {args.out}", flush=True)


if __name__ == "__main__":
    main()
