"""Self-checks for security flow input parsing and confirm-sig."""

from __future__ import annotations

from pydantic import SecretStr

from sessionvault.bot.callback_data import ConfirmCB, SecurityCB, sign_confirm, verify_confirm
from sessionvault.bot.flows.security import _has_delim_ambiguity, _split_pipes
from sessionvault.config import Settings
from sessionvault.mtproto.change_phone import _sent_code_type


def _settings() -> Settings:
    import base64
    key = base64.b64encode(b"0" * 32).decode()
    return Settings(  # type: ignore[call-arg]
        bot_token=SecretStr("x:y"),
        owner_id=1,
        pyrogram_api_id=1,
        pyrogram_api_hash=SecretStr("h"),
        master_key=SecretStr(key),
    )


def test_split_pipes_pads_to_expected() -> None:
    assert _split_pipes("a", 3) == ["a", "", ""]
    assert _split_pipes("a | b", 3) == ["a", "b", ""]
    assert _split_pipes("a|b|c|d", 3) == ["a", "b", "c"]


def test_split_pipes_trims_whitespace() -> None:
    assert _split_pipes("  pw  |  hint  ", 2) == ["pw", "hint"]


def test_has_delim_ambiguity_flags_extra_pipes() -> None:
    # 2 fields => at most 1 pipe. More = ambiguous (password contained '|').
    assert not _has_delim_ambiguity("pw", 2)
    assert not _has_delim_ambiguity("pw | hint", 2)
    assert _has_delim_ambiguity("pw|word|hint", 2)
    # 3 fields => at most 2 pipes. More = ambiguous.
    assert not _has_delim_ambiguity("a | b | c", 3)
    assert _has_delim_ambiguity("a | b | c | d", 3)


def test_confirm_sig_roundtrip() -> None:
    s = _settings()
    cb = ConfirmCB(action="twofa_enable", account_id="acc-1")
    signed = sign_confirm(cb, s)
    assert signed.sig
    assert verify_confirm(signed, s)


def test_confirm_sig_rejects_tampering() -> None:
    s = _settings()
    cb = ConfirmCB(action="twofa_enable", account_id="acc-1")
    signed = sign_confirm(cb, s)
    forged = signed.model_copy(update={"account_id": "acc-2"})
    assert not verify_confirm(forged, s)


def test_security_cb_roundtrip_via_pack() -> None:
    cb = SecurityCB(action="sr", account_id="acc-1", arg="123")
    packed = cb.pack()
    assert "sr" in packed
    parsed = SecurityCB.unpack(packed)
    assert parsed.action == "sr"
    assert parsed.arg == "123"


# --- Callback size regression: every real payload must fit in 64 bytes -----

_REAL_UUID = "550e8400-e29b-41d4-a716-446655440000"
_INT64_MAX_STR = "9223372036854775807"  # 19 digits, worst case for session ids


def test_signed_confirm_cb_fits_64_bytes() -> None:
    s = _settings()
    for action in ("twofa_enable", "twofa_change", "twofa_remove", "phone_change", "export"):
        signed = sign_confirm(ConfirmCB(action=action, account_id=_REAL_UUID), s)
        # If sig or action is too long, pack() raises ValueError > 64 bytes.
        signed.pack()


def test_security_cb_session_revoke_fits_64_bytes() -> None:
    for action in ("sr", "srD"):
        SecurityCB(action=action, account_id=_REAL_UUID, arg=_INT64_MAX_STR).pack()


def test_security_cb_menu_actions_fit_64_bytes() -> None:
    for action in (
        "menu", "twofa_enable", "twofa_change", "twofa_remove",
        "recovery_confirm", "sessions", "phone_change",
    ):
        SecurityCB(action=action, account_id=_REAL_UUID).pack()


# --- _sent_code_type: longer labels win before substrings ----------------

class _Type:
    """Stand-in for a Pyrogram SentCodeType* object; the classifier only reads
    the class name."""


def _sent(name: str) -> object:
    tp = type(name, (_Type,), {})()
    return type("Sent", (), {"type": tp})()


def test_sent_code_type_prefers_longer_labels() -> None:
    assert _sent_code_type(_sent("SentCodeTypeApp")) == "app"
    assert _sent_code_type(_sent("SentCodeTypeSms")) == "sms"
    assert _sent_code_type(_sent("SentCodeTypeCall")) == "call"
    assert _sent_code_type(_sent("SentCodeTypeFlashCall")) == "flashcall"
    assert _sent_code_type(_sent("SentCodeTypeMissedCall")) == "missedcall"
    assert _sent_code_type(_sent("SentCodeTypeFirebaseSms")) == "firebasesms"
    assert _sent_code_type(_sent("SentCodeTypeEmail")) == "email"
