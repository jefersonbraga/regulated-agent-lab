import copy

from regulated_agent_lab.controls.redaction import redact


def test_redacts_pii_keys_nested():
    value = {"passenger": {"full_name": "Anna Muster", "email": "a@x.ch"}}
    assert redact(value) == {
        "passenger": {"full_name": "[REDACTED:full_name]", "email": "[REDACTED:email]"}
    }


def test_redacts_email_in_free_text():
    out = redact("Contact anna.muster@example.ch for the ticket")
    assert out == "Contact [REDACTED:email] for the ticket"
    assert "anna.muster@example.ch" not in out


def test_redacts_ahv_and_passport_in_text():
    out = redact({"note": "AHV 756.1234.5678.97, passport X1234567", "items": ["X1234567"]})
    assert out == {
        "note": "AHV [REDACTED:document], passport [REDACTED:document]",
        "items": ["[REDACTED:document]"],
    }


def test_leaves_non_pii_untouched():
    value = {"origin": "Zurich HB", "price_cents": 5600}
    assert redact(value) == {"origin": "Zurich HB", "price_cents": 5600}


def test_does_not_mutate_input():
    value = {"passenger": {"name": "Anna", "notes": ["a@x.ch"]}, "price_cents": 5600}
    snapshot = copy.deepcopy(value)
    out = redact(value)
    assert value == snapshot
    assert out is not value
    assert out["passenger"] is not value["passenger"]


def test_redacts_undotted_ahv_in_text():
    out = redact({"reason": "AHV 7561234567897 given"})
    assert "7561234567897" not in out["reason"]
    assert "[REDACTED:document]" in out["reason"]
