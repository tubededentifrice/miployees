"""Tests for :mod:`app.util.redact`.

The redactor is the single seam every domain caller uses to scrub
PII before a log, an LLM outbound, or a user export. These tests
exercise each rule in isolation plus a cross-cutting consent
matrix; the property-based fuzzer in
``tests/property/test_redact_fuzz.py`` provides the broader
coverage.

See ``docs/specs/15-security-privacy.md`` §"Logging and redaction"
and ``docs/specs/11-llm-and-agents.md`` §"Redaction layer".
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.util.redact import ConsentSet, RedactScope, redact, scrub_string

# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


class TestEmailRedaction:
    def test_plain_email_in_string(self) -> None:
        assert redact("contact jean@example.com please", scope="log") == (
            "contact <redacted:email> please"
        )

    def test_email_with_plus_tag(self) -> None:
        out = redact("send to jean+tag@example.com today", scope="log")
        assert "jean+tag@example.com" not in out
        assert "<redacted:email>" in out

    def test_email_with_subdomain(self) -> None:
        out = redact("x@mail.staff.example.co.uk ok", scope="log")
        assert "x@mail.staff.example.co.uk" not in out
        assert "<redacted:email>" in out

    def test_email_in_nested_dict(self) -> None:
        out = redact(
            {"notes": {"body": "reach me at jean@example.com"}},
            scope="log",
        )
        assert isinstance(out, dict)
        notes = out["notes"]
        assert isinstance(notes, dict)
        assert "<redacted:email>" in notes["body"]

    def test_not_an_email_left_alone(self) -> None:
        # No TLD → no match.
        assert redact("@example without user", scope="log") == "@example without user"


# ---------------------------------------------------------------------------
# Phone
# ---------------------------------------------------------------------------


class TestPhoneRedaction:
    @pytest.mark.parametrize(
        "phone",
        [
            "+33612345678",  # FR mobile
            "+14155552671",  # US
            "+442071838750",  # UK landline
            "33612345678",  # no plus
        ],
    )
    def test_e164_like_numbers_redacted(self, phone: str) -> None:
        out = redact(f"call {phone} tomorrow", scope="log")
        assert phone not in out
        assert "<redacted:phone>" in out

    def test_short_number_not_phone(self) -> None:
        # 7 digits is below the 8-14 digit tail minimum.
        out = redact("room 12345", scope="log")
        assert out == "room 12345"

    def test_number_with_leading_zero_not_phone(self) -> None:
        # E.164 forbids a leading 0 on the country-code leading digit.
        out = redact("ref 0123456789", scope="log")
        assert "<redacted:phone>" not in out

    def test_long_numeric_run_not_phone(self) -> None:
        # 20-digit transaction reference — too long for E.164.
        out = redact("ref 12345678901234567890", scope="log")
        assert "<redacted:phone>" not in out


# ---------------------------------------------------------------------------
# IBAN (mod-97)
# ---------------------------------------------------------------------------


class TestIbanRedaction:
    # Real test IBANs published in their respective national specs /
    # Wikipedia examples. Each passes mod-97.
    @pytest.mark.parametrize(
        "iban",
        [
            "FR1420041010050500013M02606",
            "DE89370400440532013000",
            "GB82WEST12345698765432",
            "NL91ABNA0417164300",
            "CH9300762011623852957",
        ],
    )
    def test_valid_iban_redacted(self, iban: str) -> None:
        out = redact(f"wire to {iban} today", scope="log")
        assert iban not in out
        assert "<redacted:iban>" in out

    def test_iban_with_corrupted_check_digits_not_redacted(self) -> None:
        # Valid FR shape + length but failing checksum.
        bogus = "FR0000041010050500013M02606"
        out = redact(f"wire to {bogus}", scope="log")
        assert bogus in out

    def test_short_shape_not_iban(self) -> None:
        # Below the 15-char minimum. Short enough that no other regex
        # (phone, PAN) picks it up either, so it survives intact.
        too_short = "FR120"
        out = redact(f"ref {too_short}", scope="log")
        assert too_short in out
        assert "<redacted:iban>" not in out


# ---------------------------------------------------------------------------
# PAN (Luhn)
# ---------------------------------------------------------------------------


class TestPanRedaction:
    @pytest.mark.parametrize(
        "pan",
        [
            "4242424242424242",  # classic Stripe test card
            "4111111111111111",  # Visa test
            "5555555555554444",  # MC test
            "378282246310005",  # Amex test (15 digits)
        ],
    )
    def test_valid_luhn_pan_redacted(self, pan: str) -> None:
        out = redact(f"card {pan} end", scope="log")
        assert pan not in out
        assert "<redacted:pan>" in out

    def test_luhn_invalid_16_digit_not_redacted_as_pan(self) -> None:
        # 16 digits, Luhn fails → not PAN. A 16-digit run exceeds the
        # E.164 maximum (15) so the phone regex doesn't match it either
        # — the value survives intact, which is what the spec requires
        # (redact on positive Luhn only).
        out = redact("card 1234567890123456 end", scope="log")
        assert "1234567890123456" in out
        assert "<redacted:pan>" not in out

    def test_12_digit_number_not_pan(self) -> None:
        # Below PAN's 13-digit lower bound.
        out = redact("ref 123456789012 end", scope="log")
        assert "<redacted:pan>" not in out

    def test_20_digit_number_not_pan(self) -> None:
        # Above PAN's 19-digit upper bound.
        out = redact("ref 12345678901234567890 end", scope="log")
        assert "<redacted:pan>" not in out


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


class TestCredentialRedaction:
    def test_bearer_token(self) -> None:
        out = redact("auth Bearer xyz123abc", scope="log")
        assert "xyz123abc" not in out
        assert "<redacted:credential>" in out

    def test_jwt(self) -> None:
        out = redact("got jwt eyJhbG.abcDEF.ghi123 here", scope="log")
        assert "eyJhbG.abcDEF.ghi123" not in out
        assert "<redacted:credential>" in out

    def test_long_hex(self) -> None:
        hex_secret = "a" * 32 + "0" * 32  # 64 chars
        out = redact(f"t {hex_secret} end", scope="log")
        assert hex_secret not in out

    def test_short_hex_preserved(self) -> None:
        # 16 chars - below the 32 threshold.
        out = redact("short deadbeefcafebabe end", scope="log")
        assert "deadbeefcafebabe" in out


# ---------------------------------------------------------------------------
# Key-name rules
# ---------------------------------------------------------------------------


class TestKeyBasedRedaction:
    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "PASSWORD",
            "x_password",
            "token",
            "access_token",
            "api_key",
            "api-key",
            "apikey",
            "APIKEY",
            "X-API-Key",
            "cookie",
            "Set-Cookie",
            "session_id",
            "secret",
            "authorization",
            "Authorization",
            "credential",
            "passkey",
            "iban",
            "pan",
            "account_number",
            "account_number_plaintext",
        ],
    )
    def test_sensitive_key_redacted(self, key: str) -> None:
        out = redact({key: "some-value"}, scope="log")
        assert isinstance(out, dict)
        assert out[key] == "<redacted:sensitive-key>"

    def test_non_sensitive_key_preserved(self) -> None:
        out = redact({"user_id": "u1", "count": 7}, scope="log")
        assert out == {"user_id": "u1", "count": 7}

    @pytest.mark.parametrize(
        "key",
        [
            "max_tokens",
            "tokens_remaining",
            "panel_id",
            "spanner",
            "secretary",
            "cookies_accepted",
            "account_type",
            "banner_message",
        ],
    )
    def test_lookalike_key_not_matched(self, key: str) -> None:
        """Regression guard: the substring match used to swallow
        benign keys like ``max_tokens``. The whole-word rule must
        leave these alone.
        """
        out = redact({key: "real-value"}, scope="log")
        assert isinstance(out, dict)
        assert out[key] == "real-value"


class TestHashKeyPassthrough:
    """§15 PII minimisation: hashed forms survive every scope.

    Magic-link / recovery / dev-login audit rows deliberately store
    ``email_hash`` / ``ip_hash`` / ``token_hash`` instead of the
    plaintext, so forensic lookup still works after the raw value
    is purged. Redacting the hash itself (it looks like a credential
    blob to the regex) would defeat the point — hash-suffixed keys
    therefore pass through unchanged.
    """

    @pytest.mark.parametrize(
        "key",
        [
            "email_hash",
            "ip_hash",
            "EMAIL_HASH",
            "email-hash",
            "token_fingerprint",
            # Middle-word hash tokens (regression: cd-a469 magic-link
            # audit rows use ``ip_hash_at_request`` — the suffix-only
            # rule rejected this).
            "ip_hash_at_request",
            "email_hashed_for_lookup",
        ],
    )
    def test_hash_value_survives_all_scopes(self, key: str) -> None:
        sha256_hex = "a" * 64
        scopes: tuple[RedactScope, ...] = ("log", "llm", "export")
        for scope in scopes:
            out = redact({key: sha256_hex}, scope=scope)
            assert isinstance(out, dict)
            assert out[key] == sha256_hex, f"hash scrubbed under scope={scope}"

    def test_hash_value_survives_even_when_matches_credential_regex(self) -> None:
        # 64-char hex looks like a credential; the hash-key rule wins.
        out = redact({"email_hash": "deadbeef" * 8}, scope="log")
        assert isinstance(out, dict)
        assert out["email_hash"] == "deadbeef" * 8

    def test_hash_key_does_not_unlock_structural_children(self) -> None:
        # A nested dict under an ``_hash`` key still walks recursively:
        # the pass-through only fires when the value is a plain string.
        out = redact(
            {"batch_hash": {"raw": "jean@example.com"}},
            scope="log",
        )
        assert isinstance(out, dict)
        nested = out["batch_hash"]
        assert isinstance(nested, dict)
        assert nested["raw"] == "<redacted:email>"

    def test_nested_key_redacted_at_depth_2(self) -> None:
        out = redact(
            {"outer": {"Authorization": "Bearer abc"}},
            scope="log",
        )
        assert isinstance(out, dict)
        inner = out["outer"]
        assert isinstance(inner, dict)
        assert inner["Authorization"] == "<redacted:sensitive-key>"

    def test_nested_key_redacted_at_depth_3(self) -> None:
        out = redact(
            {"a": {"b": {"token": "secret-val"}}},
            scope="log",
        )
        assert isinstance(out, dict)
        layer_a = out["a"]
        assert isinstance(layer_a, dict)
        layer_b = layer_a["b"]
        assert isinstance(layer_b, dict)
        assert layer_b["token"] == "<redacted:sensitive-key>"

    def test_sensitive_value_is_secretstr(self) -> None:
        out = redact(
            {"cred": SecretStr("hunter2")},
            scope="log",
        )
        # SecretStr short-circuit fires before the key rule, but the
        # end result is identical — the secret never surfaces.
        assert isinstance(out, dict)
        assert "hunter2" not in repr(out)

    def test_secretstr_under_benign_key_still_redacted(self) -> None:
        """A :class:`SecretStr` under a NON-sensitive key still redacts.

        The type-level short-circuit fires in :func:`_redact` before
        any key check — a :class:`SecretStr` is a declared secret
        regardless of its container's name, so the scrub must fire
        even under a name like ``value`` or ``extra`` that the
        sensitive-key rule would otherwise let pass.
        """
        out = redact(
            {"value": SecretStr("hunter2"), "note": "extra"},
            scope="log",
        )
        assert isinstance(out, dict)
        assert out["value"] == "<redacted:sensitive-key>"
        assert out["note"] == "extra"
        assert "hunter2" not in repr(out)


# ---------------------------------------------------------------------------
# Consent pass-through (scope="llm")
# ---------------------------------------------------------------------------


class TestConsentPassThrough:
    def test_empty_consents_redact_everything(self) -> None:
        out = redact(
            {"legal_name": "Jean Dupont", "email": "jean@example.com"},
            scope="llm",
            consents=ConsentSet.none(),
        )
        assert isinstance(out, dict)
        # No consent → free-text scrub applies to every string leaf.
        # "Jean Dupont" has no PII shape so it survives, but the
        # email value is scrubbed.
        assert out["legal_name"] == "Jean Dupont"
        assert out["email"] == "<redacted:email>"

    def test_consent_preserves_matching_field(self) -> None:
        out = redact(
            {"legal_name": "Jean Dupont"},
            scope="llm",
            consents=ConsentSet(fields=frozenset({"legal_name"})),
        )
        assert isinstance(out, dict)
        assert out["legal_name"] == "Jean Dupont"

    def test_consent_does_not_leak_other_pii_in_same_value(self) -> None:
        """Consent permits the field name, not the value's contents.

        A user who opted in to share ``legal_name`` did not opt in
        to share an embedded email / IBAN / PAN. The regex set still
        runs inside consented values so a leaked email in the middle
        of a name stays redacted. Pure names (no PII shape) survive
        the scrub untouched — the regex is a no-op on content that
        doesn't match any pattern.
        """
        consents = ConsentSet(fields=frozenset({"legal_name"}))
        out = redact(
            {"legal_name": "Jean (jean@example.com)"},
            scope="llm",
            consents=consents,
        )
        assert isinstance(out, dict)
        # The outer name survives; the embedded email is scrubbed.
        # This is the documented stricter semantic — consent opens
        # the field, not its contents.
        assert out["legal_name"] == "Jean (<redacted:email>)"

    def test_consent_does_not_override_sensitive_key(self) -> None:
        # Even with consent, "iban" remains a sensitive key.
        consents = ConsentSet(fields=frozenset({"iban"}))
        out = redact(
            {"iban": "FR1420041010050500013M02606"},
            scope="llm",
            consents=consents,
        )
        assert isinstance(out, dict)
        assert out["iban"] == "<redacted:sensitive-key>"

    def test_consent_scrubs_embedded_iban_and_pan(self) -> None:
        """Consent for a label field does not shield embedded CRITICAL PII.

        §15 marks IBAN / PAN as CRITICAL. A user who opted in to
        share ``legal_name`` must not accidentally leak a bank
        account number wrapped in a name string.
        """
        consents = ConsentSet(fields=frozenset({"legal_name"}))
        out = redact(
            {
                "legal_name": (
                    "Jean (card 4242424242424242, IBAN FR1420041010050500013M02606)"
                )
            },
            scope="llm",
            consents=consents,
        )
        assert isinstance(out, dict)
        value = out["legal_name"]
        assert isinstance(value, str)
        assert "4242424242424242" not in value
        assert "FR1420041010050500013M02606" not in value
        assert "<redacted:pan>" in value
        assert "<redacted:iban>" in value
        assert value.startswith("Jean (")

    def test_consent_matches_allowed_name_after_scrub(self) -> None:
        """A pure name under consent survives the scrub untouched."""
        consents = ConsentSet(fields=frozenset({"legal_name"}))
        out = redact(
            {"legal_name": "Maria Santos"},
            scope="llm",
            consents=consents,
        )
        assert isinstance(out, dict)
        assert out["legal_name"] == "Maria Santos"

    def test_consent_ignored_at_log_scope(self) -> None:
        consents = ConsentSet(fields=frozenset({"legal_name"}))
        out = redact(
            {"legal_name": "Jean Dupont"},
            scope="log",
            consents=consents,
        )
        assert isinstance(out, dict)
        # "Jean Dupont" has no PII shape, survives scope=log regardless.
        assert out["legal_name"] == "Jean Dupont"

    def test_consent_ignored_at_log_scope_with_pii_value(self) -> None:
        consents = ConsentSet(fields=frozenset({"email"}))
        out = redact(
            {"email": "jean@example.com"},
            scope="log",
            consents=consents,
        )
        assert isinstance(out, dict)
        # scope=log ignores consent; the email is scrubbed.
        assert out["email"] == "<redacted:email>"

    def test_consent_ignored_at_export_scope(self) -> None:
        consents = ConsentSet(fields=frozenset({"legal_name"}))
        out = redact(
            {"legal_name": "Jean", "email": "jean@example.com"},
            scope="export",
            consents=consents,
        )
        assert isinstance(out, dict)
        assert out["email"] == "<redacted:email>"

    def test_structural_value_still_walked_under_consent(self) -> None:
        """A nested dict under an allowed key is still scrubbed recursively.

        Consent pass-through only fires when the value is a direct
        string leaf. A nested mapping still walks so a leaked email
        one level deeper is caught.
        """
        consents = ConsentSet(fields=frozenset({"legal_name"}))
        out = redact(
            {"legal_name": {"raw": "jean@example.com"}},
            scope="llm",
            consents=consents,
        )
        assert isinstance(out, dict)
        raw_layer = out["legal_name"]
        assert isinstance(raw_layer, dict)
        assert raw_layer["raw"] == "<redacted:email>"


# ---------------------------------------------------------------------------
# Multimodal image-block carve-out
# ---------------------------------------------------------------------------


class TestImageBlockCarveOut:
    """``{"type": "image_url", ...}`` blocks skip the credential regex.

    OpenAI / OpenRouter vision requests wrap the image in a base64
    data URL that routinely matches the credential-shape regex.
    Scrubbing the bytes would silently break every vision call, so
    the redactor leaves the ``url`` leaf intact. Sibling keys (the
    prompt text block, the outer ``type`` discriminator, …) still
    run through the regular rules so a stray PII leak next to the
    image is still caught.
    """

    def test_base64_image_url_survives(self) -> None:
        # 64 'A's is well above the 40-char base64url threshold, so
        # the credential regex WOULD fire without the carve-out.
        fake_payload = "A" * 64
        data_url = f"data:image/jpeg;base64,{fake_payload}"
        block = {"type": "image_url", "image_url": {"url": data_url}}
        out = redact(block, scope="llm")
        assert isinstance(out, dict)
        image_url = out["image_url"]
        assert isinstance(image_url, dict)
        assert image_url["url"] == data_url

    def test_sibling_text_block_still_scrubs_pii(self) -> None:
        # Messages list with two content blocks: a PII-laden text
        # block and an image block. The text must still be scrubbed.
        payload = {
            "role": "user",
            "content": [
                {"type": "text", "text": "email leak@example.com"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64," + "A" * 64},
                },
            ],
        }
        out = redact(payload, scope="llm")
        assert isinstance(out, dict)
        content = out["content"]
        assert isinstance(content, list)
        text_block = content[0]
        assert isinstance(text_block, dict)
        assert "<redacted:email>" in text_block["text"]
        image_block = content[1]
        assert isinstance(image_block, dict)
        image_url = image_block["image_url"]
        assert isinstance(image_url, dict)
        assert image_url["url"].endswith("A" * 64)

    def test_image_block_type_field_is_only_discriminator(self) -> None:
        # A domain dict that happens to carry ``type: "image_url"``
        # but is NOT a multimodal block (no ``image_url`` sibling
        # holding a URL) must still be scrubbed. The carve-out fires
        # on the tight shape, and its effect is limited to the
        # ``image_url`` sibling — stray strings elsewhere still
        # route through the regex.
        payload = {
            "type": "image_url",
            "note": "contact jean@example.com",
        }
        out = redact(payload, scope="llm")
        assert isinstance(out, dict)
        assert "<redacted:email>" in out["note"]

    def test_non_image_block_dict_not_affected(self) -> None:
        # A plain dict that happens to have a ``type`` field with a
        # non-image value is walked normally.
        payload = {
            "type": "text",
            "text": "call +33612345678",
        }
        out = redact(payload, scope="llm")
        assert isinstance(out, dict)
        assert "<redacted:phone>" in out["text"]


# ---------------------------------------------------------------------------
# Structural recursion
# ---------------------------------------------------------------------------


class TestStructuralRecursion:
    def test_preserves_dict(self) -> None:
        out = redact({"k": "v"}, scope="log")
        assert isinstance(out, dict)

    def test_preserves_list(self) -> None:
        out = redact(["a", "b"], scope="log")
        assert isinstance(out, list)

    def test_preserves_tuple(self) -> None:
        out = redact(("a", "b"), scope="log")
        assert isinstance(out, tuple)

    def test_preserves_set(self) -> None:
        out = redact({"a", "b"}, scope="log")
        assert isinstance(out, set)

    def test_preserves_frozenset(self) -> None:
        out = redact(frozenset({"a", "b"}), scope="log")
        assert isinstance(out, frozenset)

    def test_recurses_into_list(self) -> None:
        out = redact(["plain", "Bearer abc123xyz"], scope="log")
        assert isinstance(out, list)
        assert out[0] == "plain"
        assert "abc123xyz" not in out[1]

    def test_recurses_into_tuple(self) -> None:
        out = redact(("plain", "jean@example.com"), scope="log")
        assert isinstance(out, tuple)
        assert out[0] == "plain"
        assert "jean@example.com" not in out[1]

    def test_deep_nesting_beyond_cap_string_scrubbed(self) -> None:
        """Depths past :data:`_MAX_DEPTH` are repr'd and string-scrubbed."""
        nested: object = "Bearer deep-nested-123"
        for _ in range(20):
            nested = {"x": nested}
        out = redact(nested, scope="log")
        # Walk back down to the bottom string; it must not contain
        # the raw secret. Because past the cap we collapse subtrees
        # to a scrubbed repr string, the exact structure diverges
        # from the input at the cap — we assert on full-text safety.
        assert "deep-nested-123" not in repr(out)

    def test_deep_nesting_email_leaf_scrubbed_via_repr_fallback(self) -> None:
        """An email buried 20-deep is still caught by the repr fallback."""
        nested: object = "leak@example.com"
        for _ in range(20):
            nested = {"x": nested}
        out = redact(nested, scope="log")
        # The repr fallback collapses the sub-tree past the cap,
        # then runs string-scrub over the Python dict-repr. The
        # email shape is detectable in the dict-repr form, so the
        # literal must not appear in the final output.
        assert "leak@example.com" not in repr(out)

    def test_deep_structure_does_not_recurse_to_python_limit(self) -> None:
        """A 1_000-deep dict must not blow the stack."""
        nested: object = "bottom"
        for _ in range(1_000):
            nested = {"k": nested}
        # Must return without raising; assertion on shape would be
        # fragile given the cap fallback.
        redact(nested, scope="log")


class TestPrimitiveLeaves:
    def test_none(self) -> None:
        assert redact(None, scope="log") is None

    def test_empty_dict(self) -> None:
        assert redact({}, scope="log") == {}

    def test_empty_list(self) -> None:
        assert redact([], scope="log") == []

    def test_int_float_bool_pass_through(self) -> None:
        out = redact({"i": 7, "f": 3.14, "b": True}, scope="log")
        assert out == {"i": 7, "f": 3.14, "b": True}


# ---------------------------------------------------------------------------
# Purity + determinism
# ---------------------------------------------------------------------------


class TestPurityAndDeterminism:
    def test_input_not_mutated(self) -> None:
        original = {"email": "jean@example.com", "items": ["Bearer abc123def456"]}
        snapshot = {"email": "jean@example.com", "items": ["Bearer abc123def456"]}
        redact(original, scope="log")
        assert original == snapshot

    def test_idempotent(self) -> None:
        payload = {"email": "jean@example.com", "note": "call +33612345678"}
        first = redact(payload, scope="log")
        second = redact(payload, scope="log")
        assert first == second


# ---------------------------------------------------------------------------
# scope semantics cross-check
# ---------------------------------------------------------------------------


class TestScopeSemantics:
    def test_log_and_llm_identical_without_consent(self) -> None:
        payload = {
            "email": "jean@example.com",
            "phone": "+33612345678",
            "password": "hunter2",
        }
        log_out = redact(payload, scope="log")
        llm_out = redact(payload, scope="llm")
        assert log_out == llm_out

    def test_export_redacts_free_text(self) -> None:
        # scope=export is the "defensive sweep" for exporter output.
        payload = {"body": "email me: jean@example.com"}
        out = redact(payload, scope="export")
        assert isinstance(out, dict)
        assert out["body"] == "email me: <redacted:email>"


# ---------------------------------------------------------------------------
# scrub_string public helper
# ---------------------------------------------------------------------------


class TestScrubString:
    """scrub_string is exposed so logging.py can reuse the free-text pass
    on its already-formatted message text. Smoke-test the public surface
    here — full coverage lives in the regex-specific test classes above.
    """

    def test_covers_email_and_bearer_in_one_pass(self) -> None:
        out = scrub_string("auth Bearer abcdef call jean@example.com")
        assert "Bearer abcdef" not in out
        assert "jean@example.com" not in out
        assert "<redacted:email>" in out
        assert "<redacted:credential>" in out

    def test_empty_string_returns_empty(self) -> None:
        assert scrub_string("") == ""
