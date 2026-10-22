"""Self-check for profile_extras input parsers and result formatter."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sessionvault.bot.flows.profile_extras import _parse_birthday, _parse_channel
from sessionvault.domain.profile import format_op_result
from sessionvault.storage.db.models import ProfileOperationState


def _op(state: ProfileOperationState, error_code: str | None = None) -> object:
    return SimpleNamespace(state=state, error_code=error_code)


def test_format_ok() -> None:
    assert format_op_result(_op(ProfileOperationState.OK)) == "Готово."  # type: ignore[arg-type]


def test_format_unsupported_marker_wrapped() -> None:
    msg = format_op_result(_op(ProfileOperationState.FAILED, "USERNAME_INVALID"))  # type: ignore[arg-type]
    assert "unsupported" in msg


def test_format_locked() -> None:
    msg = format_op_result(_op(ProfileOperationState.FAILED, "ACCOUNT_LOCKED"))  # type: ignore[arg-type]
    assert "занят" in msg


def test_format_generic() -> None:
    msg = format_op_result(_op(ProfileOperationState.FAILED, "FLOOD_WAIT_60"))  # type: ignore[arg-type]
    assert "FLOOD_WAIT_60" in msg
    assert "unsupported" not in msg


def test_birthday_full_date() -> None:
    op, payload = _parse_birthday("05.03.1990")
    assert op == "profile.birthday.set"
    assert payload == {"day": 5, "month": 3, "year": 1990}


def test_birthday_no_year() -> None:
    op, payload = _parse_birthday("5/3")
    assert op == "profile.birthday.set"
    assert payload == {"day": 5, "month": 3}


def test_birthday_two_digit_year_expands() -> None:
    _, payload = _parse_birthday("05.03.90")
    assert payload["year"] == 1990
    _, payload = _parse_birthday("05.03.05")
    assert payload["year"] == 2005


def test_birthday_clear() -> None:
    assert _parse_birthday("-") == ("profile.birthday.clear", {})


@pytest.mark.parametrize("bad", ["", "abc", "32.01", "05.13.1990", "5"])
def test_birthday_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        _parse_birthday(bad)


def test_channel_username_strips_at() -> None:
    assert _parse_channel("@mychannel") == ("profile.personal_channel.set", {"chat": "mychannel"})


def test_channel_numeric_id() -> None:
    assert _parse_channel("-1001234567890") == (
        "profile.personal_channel.set",
        {"chat": -1001234567890},
    )


def test_channel_clear() -> None:
    assert _parse_channel("-") == ("profile.personal_channel.clear", {})
    assert _parse_channel("") == ("profile.personal_channel.clear", {})
