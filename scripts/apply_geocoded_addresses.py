#!/usr/bin/env python3
"""Apply reviewed geocode proposals to the customer records, with a strict gate.

Dry-run by default; --commit writes. Only fills/corrects a field when the geocode
result can be corroborated, because Nominatim confidently returns WRONG matches for
South African informal addresses (real examples found while testing):

  "3806 Waterfall Crest; 25 Jose Street, Vorna Valley"  ->  Thulamela, LIMPOPO   (wrong province)
  "53 Humewood Drive Parklands Capetown"                ->  Station Deck, Foreshore (wrong street)

Gate rules
----------
1. precision 'none'            -> never touched.
2. province/postcode/city/suburb -> applied when the result is at least area level and
   its province does not contradict a province named in the source address.
3. address_line1 (the street)  -> applied ONLY when the matched road shares a token
   with the source address, so a confident-looking wrong street cannot overwrite it.
4. Existing values are never blanked; anything replaced is recoverable because the
   original raw address is kept in booqable_address_raw (fallback rows keep it in
   address_line1 via --keep-raw).

Writes nothing unless --commit is passed.
"""
import argparse
import csv
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from booqable_db import connect, finish  # noqa: E402

BASE = os.path.expanduser("~/abi-backups/booqable-import")
PROPOSALS = os.path.join(BASE, "address-proposals.csv")
REVIEW = os.path.join(BASE, "address-apply-review.csv")

PROVINCES = ["gauteng", "kwazulu-natal", "kwazulu natal", "western cape", "eastern cape",
             "free state", "limpopo", "mpumalanga", "north west", "northern cape"]
STOP = {"street", "road", "avenue", "ave", "drive", "dr", "close", "crescent", "cres",
        "lane", "way", "place", "pl", "court", "ext", "extension", "no", "the", "and",
        "south", "africa", "plot", "stand", "house", "flat", "unit", "complex", "park"}

# Queries built only from numbers/fragments ("1686, South Africa", "20191, South Africa",
# "ex45, 1666, South Africa") are dangerously ambiguous - they matched Gauteng addresses to
# Thulamela, Limpopo. A query is only trusted if it still contains a real place word.
COUNTRY_WORDS = {"south", "africa", "rsa", "sa"}


def query_is_reliable(query):
    """False when the query has no meaningful place word left in it."""
    words = [w for w in re.findall(r"[A-Za-z]{3,}", query or "")
             if w.lower() not in COUNTRY_WORDS]
    return bool(words)

# Nominatim often returns a municipality instead of a town. Tidy it for display.
CITY_LABEL_CLEANERS = [
    (re.compile(r"^city of (.+?) metropolitan municipality$", re.I), r"\1"),
    (re.compile(r"^(.+?) metropolitan municipality$", re.I), r"\1"),
    (re.compile(r"^(.+?) local municipality$", re.I), r"\1"),
    (re.compile(r"^(.+?) district municipality$", re.I), r"\1"),
]


def clean_city(value):
    text = (value or "").strip()
    for pattern, replacement in CITY_LABEL_CLEANERS:
        new = pattern.sub(replacement, text)
        if new != text:
            text = new
            break
    if text.islower() or text.isupper():
        text = text.title()
    return text.strip()


def tokens(text):
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) > 2 and t not in STOP}


def province_in(text):
    low = (text or "").lower()
    return [p.title() for p in PROVINCES if p in low]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proposals", default=PROPOSALS)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--database")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--review-out", default=REVIEW)
    args = parser.parse_args()

    proposals = [r for r in csv.DictReader(open(args.proposals, encoding="utf-8-sig"))]
    print(f"proposals: {len(proposals)}")

    db = connect(args.database, args.live)
    by_source = {}
    for row in db.query("SELECT id, source_id FROM customers WHERE COALESCE(source_id,'') <> ''"):
        by_source[row["source_id"]] = row["id"]

    for table in ("customers",):
        if "source_id" not in db.table_columns(table):
            print("customers table has no source_id - wrong database?")
            db.close()
            finish(1)

    accepted, rejected = [], []
    for p in proposals:
        if p["precision"] == "none":
            rejected.append((p, "no usable match"))
            continue
        if not query_is_reliable(p.get("matched_query")):
            rejected.append((p, "query had no place word - unreliable match"))
            continue
        p["city"] = clean_city(p["city"])
        src = p["raw_address"]
        src_low = src.lower()
        src_prov = province_in(src)
        if src_prov and p["province"] and p["province"].lower() not in [s.lower() for s in src_prov]:
            rejected.append((p, f"province contradicts source ({p['province']} vs {src_prov})"))
            continue

        street = p["address_line1"].strip()
        street_ok = False
        if street:
            road_tokens = tokens(re.sub(r"^\d+[a-z]?\s*", "", street))
            shared = road_tokens & tokens(src)
            street_ok = bool(shared)
            if not street_ok:
                # the only acceptable unshared street is when the source had no street data at all
                street_ok = not tokens(src)

        # Suburb is the least reliable field: Nominatim happily returns a neighbouring
        # suburb (Parklands -> Foreshore, Linbro Park -> Lakeside). Only keep it when the
        # source address actually mentions it.
        suburb = p["suburb"].strip()
        if suburb and not (tokens(suburb) & tokens(src)):
            suburb = ""

        accepted.append((p, street if street_ok else "", suburb))

    print(f"accepted: {len(accepted)}   rejected: {len(rejected)}")
    fields_filled = {"address_line1": 0, "suburb": 0, "city": 0, "province": 0, "postal_code": 0}
    for p, street, suburb in accepted:
        if street:
            fields_filled["address_line1"] += 1
        if suburb:
            fields_filled["suburb"] += 1
        for f in ("city", "province", "postal_code"):
            if p[f].strip():
                fields_filled[f] += 1
    print("fields with a value:", fields_filled)

    with open(args.review_out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["customer_id", "name", "raw_address", "new_line1", "new_suburb", "new_city",
                    "new_province", "new_postal", "precision", "decision", "reason"])
        for p, street, suburb in accepted:
            w.writerow([p["booqable_number"], p["name"], p["raw_address"], street, suburb,
                        p["city"], p["province"], p["postal_code"], p["precision"], "apply", ""])
        for p, why in rejected:
            w.writerow([p["booqable_number"], p["name"], p["raw_address"], "", "", "", "", "",
                        p["precision"], "skip", why])
    print("review file:", args.review_out)

    if not args.commit:
        print("\nDRY RUN - nothing written. Re-run with --commit to apply.")
        db.close()
        finish(0)

    # snapshot the current values of every customer we are about to change - the libsql
    # adapter autocommits, so there is no transaction to roll back
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup_path = os.path.expanduser(f"~/abi-backups/address-apply-backup-{stamp}.json")
    os.makedirs(os.path.dirname(backup_path), exist_ok=True)
    affected = [by_source.get(p["booqable_id"]) for p, _s, _su in accepted]
    affected = [c for c in affected if c]
    cols = ("id", "source_id", "name", "address_line1", "suburb", "city", "province", "postal_code")
    before = []
    for chunk_start in range(0, len(affected), 200):
        chunk = affected[chunk_start:chunk_start + 200]
        placeholders = ",".join("?" for _ in chunk)
        for row in db.query(
                f"SELECT {', '.join(cols)} FROM customers WHERE id IN ({placeholders})", tuple(chunk)):
            before.append({c: row[c] for c in cols})
    with open(backup_path, "w") as handle:
        json.dump({"taken_at": stamp, "reason": "pre-geocode-apply snapshot",
                   "customers": before}, handle, indent=2, default=str)
    print(f"snapshot written: {backup_path} ({len(before)} customer rows)")

    updated = 0
    for p, street, suburb in accepted:
        cid = by_source.get(p["booqable_id"])
        if not cid:
            continue
        sets, params = [], []
        if street:
            sets.append("address_line1 = ?")
            params.append(street)
        for col, value in (("suburb", suburb), ("city", p["city"]), ("province", p["province"]),
                           ("postal_code", p["postal_code"])):
            value = (value or "").strip()
            if value:
                sets.append(f"{col} = ?")
                params.append(value)
        if not sets:
            continue
        params.append(cid)
        db.exec(f"UPDATE customers SET {', '.join(sets)} WHERE id = ?", tuple(params))
        updated += 1
        if updated % 250 == 0:
            print(f"  updated {updated} …", flush=True)
    print(f"updated {updated} customer record(s)")
    db.close()
    finish(0)


if __name__ == "__main__":
    main()
