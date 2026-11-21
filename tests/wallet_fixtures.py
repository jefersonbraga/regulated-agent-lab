"""Helpers for wallet tests: seed a saved card the way a completed setup would store it.

Not a test module (no ``test_`` prefix): imported by tests that need a card in the wallet.
"""

from mcp_servers.wallet import vault

LOCAL_PAYER = "local-dev"
PM_VISA = "pm_test_visa4242"
CUSTOMER = "cus_test0001"


def seed_card(
    user: str = LOCAL_PAYER,
    *,
    payment_method_id: str = PM_VISA,
    customer_id: str = CUSTOMER,
    brand: str = "visa",
    last4: str = "4242",
    exp_month: int = 12,
    exp_year: int = 2034,
) -> dict:
    """A card reference in the wallet for ``user`` (ids, brand, last four, expiry only)."""
    wallet = vault.store()
    customer = wallet.set_customer(user, customer_id)
    card, _ = wallet.save_card(
        user,
        {
            "customer_id": customer,
            "payment_method_id": payment_method_id,
            "brand": brand,
            "last4": last4,
            "exp_month": exp_month,
            "exp_year": exp_year,
        },
    )
    return card
