"""Tests for the one-off Booqable customer import (2026-09-12)."""
import json

from app.services.booqable_import import (
    AUDIT_KEYS, infer_customer_type, map_customer_row, split_address, to_local_datetime,
    validate,
)
from app.services.customers import HIDDEN_CUSTOM_FIELD_KEYS


# --------------------------------------------------------------------------- #
# address splitting - the rules that a naive parser got dangerously wrong
# --------------------------------------------------------------------------- #

def test_house_number_is_not_mistaken_for_a_postal_code():
    result = split_address("Diepsloot Ext 5 Makhulong Street House No 4234")
    assert result["postal_code"] == ""
    assert "4234" in result["address_line1"]


def test_leading_street_number_is_not_mistaken_for_a_postal_code():
    result = split_address(" 1511 Aster Street Riversideview")
    assert result["postal_code"] == ""
    assert result["address_line1"].startswith("1511 Aster Street")


def test_address_line1_is_never_empty_when_the_source_has_text():
    for raw in [
        "24 Madelaine Street Gaylee Blackheath 7580 Cape Town",
        "Oliven., South Africa",
        "Diepsloot Ext 13",
        "MIdrand, Gauteng, South Africa",
        "  ",
    ]:
        assert split_address(raw)["address_line1"].strip() != "" or not raw.strip()


def test_structured_comma_address_splits_into_fields():
    result = split_address("44 Richards Drive Halfway House Midrand, 1685")
    assert result["postal_code"] == "1685"
    assert result["city"] == "Midrand"
    assert result["address_line1"] == "44 Richards Drive Halfway House"


def test_city_and_province_and_suburb_are_extracted():
    result = split_address("8 Kings road , Wendywood, Midrand, Gauteng, South Africa")
    assert result["city"] == "Midrand"
    assert result["province"] == "Gauteng"
    assert result["suburb"] == "Wendywood"
    assert result["address_line1"] == "8 Kings road"


def test_province_only_matches_a_whole_segment():
    # "Cape Town" must not be read as the Western Cape province.
    result = split_address("12 Loop Street, Cape Town")
    assert result["city"] == "Cape Town"
    assert result["province"] == ""


def test_po_box_never_yields_a_postal_code():
    result = split_address("PO Box 1234, Midrand")
    assert result["postal_code"] == ""
    assert "1234" in result["address_line1"]


def test_fallback_keeps_the_original_text_verbatim():
    raw = "Diepsloot ext 2 723 R.M MAKHADO CLOSE"
    result = split_address(raw)
    assert result["confidence"] == "fallback"
    assert result["address_line1"] == raw


def test_no_text_is_lost_except_country_words():
    raw = "629 Uthukela atreet, Midrand, Gauteng, South Africa"
    result = split_address(raw)
    combined = " ".join([result["address_line1"], result["suburb"], result["city"],
                         result["province"], result["postal_code"]]).lower()
    for token in ["629", "uthukela", "atreet", "midrand", "gauteng"]:
        assert token in combined


# --------------------------------------------------------------------------- #
# row mapping
# --------------------------------------------------------------------------- #

ROW = {
    "id": "000cf2a2-5e63-4b64-baee-4d0fcba41cd5",
    "number": "911",
    "name": "Solomon Moloto",
    "email": "",
    "discount_percentage": "0.0",
    "deposit_type": "percentage",
    "deposit_value": "100.0",
    "email_marketing_consented": "false",
    "balance_due_in_cents": "3031100",
    "latest_order_at": "2022-12-12 15:38:41 +0200",
    "tags": "",
    "main": "Diepsloot Ext 5 Makhulong Street House No 4234",
    "phone": "+27722382055",
    "client_verification": "Yes",
    "alternative_contact_person": "David",
    "alternative_contact_number": "0824885591",
    "created_at": "2022-12-12 15:42:28 +0200",
    "updated_at": "2022-12-12 15:42:28 +0200",
    "customer_vat_no": "",
    "company_reg_no": "",
    "your_reference": "",
    "vehicle_make": "",
    "vehicle_colour": "White",
    "vehicle_registration": "caa102239",
}


def test_row_maps_core_columns():
    mapped = map_customer_row(ROW)
    assert mapped["name"] == "Solomon Moloto"
    assert mapped["phone"] == "+27722382055"
    assert mapped["email"] == ""
    assert mapped["country"] == "South Africa"
    assert mapped["customer_type"] == "individual"
    assert mapped["marketing_opt_in"] == 0
    assert mapped["created_at"] == "2022-12-12T15:42:28"
    assert mapped["source_system"] == "booqable"
    assert mapped["source_id"] == ROW["id"]


def test_row_never_carries_an_opening_balance():
    mapped = map_customer_row(ROW)
    assert "balance_due" not in mapped          # the script writes an explicit 0
    assert mapped["_custom"]["booqable_balance_due_cents"] == "3031100"


def test_vehicle_colour_maps_to_the_us_spelling_key():
    mapped = map_customer_row(ROW)
    assert mapped["_custom"]["vehicle_color"] == "White"
    assert mapped["_custom"]["vehicle_reg_no"] == "CAA102239"


def test_marketing_consent_true_becomes_one():
    mapped = map_customer_row(dict(ROW, email_marketing_consented="true"))
    assert mapped["marketing_opt_in"] == 1


def test_discount_percentage_is_carried_over():
    mapped = map_customer_row(dict(ROW, discount_percentage="10.0"))
    assert mapped["standard_discount_percent"] == 10.0


def test_custom_fields_json_is_valid_and_keeps_audit_keys():
    mapped = map_customer_row(ROW)
    payload = json.loads(mapped["custom_fields_json"])
    assert payload["alternative_contact_name"] == "David"
    assert payload["booqable_number"] == "911"
    assert payload["booqable_deposit_type"] == "percentage"


def test_audit_keys_are_hidden_from_the_ui():
    for key in AUDIT_KEYS:
        assert key in HIDDEN_CUSTOM_FIELD_KEYS


# --------------------------------------------------------------------------- #
# inference + helpers
# --------------------------------------------------------------------------- #

def test_company_inferred_from_registration_numbers():
    assert infer_customer_type("Dwyka", "4890123456", "") == "company"
    assert infer_customer_type("Dwyka", "", "2018/521057/07") == "company"


def test_company_inferred_from_business_name():
    assert infer_customer_type("LANTE DELIVERY SERVICE") == "company"
    assert infer_customer_type("Metro Events Logistics") == "company"


def test_plain_person_stays_individual():
    assert infer_customer_type("Solomon Moloto") == "individual"
    assert infer_customer_type("aa") == "individual"


def test_datetime_conversion_handles_the_export_format():
    assert to_local_datetime("2023-02-02 10:32:55 +0200") == "2023-02-02T10:32:55"
    assert to_local_datetime("2026-09-11T14:33:08") == "2026-09-11T14:33:08"
    assert to_local_datetime("") == ""


def test_validation_flags_a_missing_name():
    mapped = map_customer_row(dict(ROW, name="  "))
    assert "missing name" in validate(mapped)
