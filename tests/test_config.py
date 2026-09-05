"""Production configuration guard.

Development placeholders that reach production are the failure mode here: they
work in staging and are a breach in production. The guard turns that into a
refusal to boot, so these tests pin both directions — it must fire on a
placeholder and must stay out of the way everywhere else.
"""

from __future__ import annotations

import secrets

import pytest

from app.config import Settings

# `_env_file=None` throughout: without it pydantic-settings reads the developer's
# real .env and the test would assert against their machine, not the argument.
PROD = {
    "environment": "production",
    "jwt_secret": secrets.token_urlsafe(64),
    "cors_allowed_origins": "https://shop.example.com",
    "whatsapp_app_secret": "real-app-secret",
    "whatsapp_verify_token": "real-verify-token",
    "cashfree_webhook_secret": "real-webhook-secret",
    "debug": False,
}


def build(**overrides) -> Settings:
    return Settings(_env_file=None, **{**PROD, **overrides})


def test_a_complete_production_config_boots():
    assert build().is_production is True


@pytest.mark.parametrize(
    "override,expected",
    [
        ({"jwt_secret": "CHANGE_ME_LONG_RANDOM_STRING"}, "placeholder"),
        ({"jwt_secret": "tooshort"}, "at least 32"),
        ({"cors_allowed_origins": "*"}, "must not be '*'"),
        ({"cors_allowed_origins": ""}, "must list the storefront"),
        ({"debug": True}, "DEBUG must be false"),
        ({"whatsapp_app_secret": ""}, "WHATSAPP_APP_SECRET"),
        ({"whatsapp_verify_token": "CHANGE_ME_WEBHOOK_VERIFY_TOKEN"}, "WHATSAPP_VERIFY_TOKEN"),
        ({"cashfree_webhook_secret": ""}, "CASHFREE_WEBHOOK_SECRET"),
    ],
)
def test_production_rejects_placeholders(override, expected):
    with pytest.raises(ValueError, match=expected):
        build(**override)


def test_every_problem_is_reported_at_once():
    """One deploy attempt should reveal the whole list, not the first item."""
    with pytest.raises(ValueError) as exc:
        build(jwt_secret="CHANGE_ME", cors_allowed_origins="*", debug=True)

    message = str(exc.value)
    assert "JWT_SECRET" in message
    assert "CORS_ALLOWED_ORIGINS" in message
    assert "DEBUG" in message


def test_development_is_left_alone():
    """The same placeholders are fine locally — that is the point of them."""
    dev = Settings(
        _env_file=None,
        environment="development",
        jwt_secret="CHANGE_ME_LONG_RANDOM_STRING",
        cors_allowed_origins="*",
        debug=True,
    )
    assert dev.is_production is False


def test_staging_is_not_treated_as_production():
    """Only `production` is guarded; staging deliberately stays permissive."""
    staging = Settings(
        _env_file=None,
        environment="staging",
        jwt_secret="CHANGE_ME_LONG_RANDOM_STRING",
        cors_allowed_origins="*",
    )
    assert staging.is_production is False
