"""Stage-10: KB sync tests (Issue F-8).

Assert that key values in ``_KB_HARDWARE_POLICY`` (embedded in
``orchestrator.chat.public_handler``) remain consistent with the canonical seed
data in ``it_server.service.store._SEED_HARDWARE_POLICY``.

When the hardware policy changes, **both** files must be updated together.
These tests will fail if one is updated without the other, surfacing the drift.
"""

import pytest

from it_server.service.store import _SEED_HARDWARE_POLICY  # noqa: PLC2701
from orchestrator.chat.public_handler import _KB_HARDWARE_POLICY


@pytest.fixture(scope="module")
def seed() -> dict:
    return _SEED_HARDWARE_POLICY


def test_kb_contains_standard_laptop(seed: dict) -> None:
    """Default-kit laptop model must appear in the embedded KB text."""
    laptop = seed["standard_allocation"]["default_kit"]["laptop"]
    assert laptop in _KB_HARDWARE_POLICY, (
        f"Standard laptop {laptop!r} not found in _KB_HARDWARE_POLICY — "
        "update orchestrator/chat/public_handler.py to match it_server/service/store.py."
    )


def test_kb_contains_engineer_laptop(seed: dict) -> None:
    """Engineer/Developer laptop model (higher RAM) must appear in the KB."""
    laptop = seed["role_overrides"]["Engineer / Developer"]["laptop"]
    assert laptop in _KB_HARDWARE_POLICY, (
        f"Engineer laptop {laptop!r} not found in _KB_HARDWARE_POLICY — "
        "update orchestrator/chat/public_handler.py to match it_server/service/store.py."
    )


def test_kb_contains_executive_laptop(seed: dict) -> None:
    """Management/Executive laptop model must appear in the KB."""
    laptop = seed["role_overrides"]["Management / Executive"]["laptop"]
    assert laptop in _KB_HARDWARE_POLICY, (
        f"Executive laptop {laptop!r} not found in _KB_HARDWARE_POLICY — "
        "update orchestrator/chat/public_handler.py to match it_server/service/store.py."
    )


def test_kb_contains_laptop_replacement_years(seed: dict) -> None:
    """Laptop replacement cycle (years) must be referenced in the KB."""
    years = seed["replacement_cycle"]["laptop"]["years"]
    assert str(years) in _KB_HARDWARE_POLICY, (
        f"Laptop replacement cycle {years} year(s) not found in _KB_HARDWARE_POLICY — "
        "update orchestrator/chat/public_handler.py to match it_server/service/store.py."
    )


def test_kb_contains_phone_replacement_years(seed: dict) -> None:
    """Phone replacement cycle (years) must be referenced in the KB."""
    years = seed["replacement_cycle"]["phone"]["years"]
    assert str(years) in _KB_HARDWARE_POLICY, (
        f"Phone replacement cycle {years} year(s) not found in _KB_HARDWARE_POLICY — "
        "update orchestrator/chat/public_handler.py to match it_server/service/store.py."
    )
