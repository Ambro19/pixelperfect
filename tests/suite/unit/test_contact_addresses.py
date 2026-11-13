# tests/suite/unit/test_contact_addresses.py
# =============================================================================
# Customer-facing messages name support@pixelperfectapi.net (a Cloudflare
# Email Routing address), never the private Gmail inbox behind it.
# =============================================================================
import ast
from pathlib import Path

import main

BACKEND = Path(__file__).resolve().parents[3]
GMAIL = "onetechly@gmail.com"


def _string_constants(path: Path):
    """Every string literal in the file, including the pieces of f-strings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


def test_default_support_address_is_the_routed_one():
    assert main.SUPPORT_EMAIL == "support@pixelperfectapi.net"


def test_no_message_in_main_shows_the_gmail_address():
    # The one allowed use is the contact-form recipient default: that is where
    # mail is delivered, not something a customer is told.
    allowed = {ln for ln, v in _string_constants(BACKEND / "main.py") if v == GMAIL}
    leaks = [(ln, v) for ln, v in _string_constants(BACKEND / "main.py")
             if GMAIL in v and ln not in allowed]
    assert leaks == []
    assert len(allowed) <= 1


def test_contact_form_failure_points_to_support(client, monkeypatch):
    def boom(*a):
        raise OSError("smtp down")

    monkeypatch.setattr(main, "_send_email_sync", boom)
    r = client.post("/contact", json={"name": "N", "email": "n@example.com",
                                      "subject": "s", "message": "m"})
    assert r.status_code == 503
    assert "support@pixelperfectapi.net" in r.json()["detail"]
    assert GMAIL not in r.text


def test_deletion_refusal_points_to_support(client, make_user, fake_stripe):
    u = make_user("pro", stripe_customer_id="cus_D")
    fake_stripe.subscriptions["cus_D"] = [{"id": "sub_D", "status": "active"}]
    fake_stripe.fail_subscription_list = True
    r = client.delete("/user/delete_account", headers=u.headers)
    assert r.status_code == 503
    assert "support@pixelperfectapi.net" in r.json()["detail"]
    assert GMAIL not in r.text
