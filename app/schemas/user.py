from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import ORMModel

PINCODE_RE = re.compile(r"^\d{6}$")
PHONE_RE = re.compile(r"^\+?[1-9]\d{7,14}$")
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z]{1}[A-Z\d]{1}[Z]{1}[A-Z\d]{1}$")


class AddressBase(BaseModel):
    label: str = Field(default="Home", max_length=50)
    recipient_name: str | None = Field(default=None, max_length=255)
    recipient_phone: str | None = Field(default=None, max_length=20)
    line1: str = Field(min_length=3, max_length=255)
    line2: str | None = Field(default=None, max_length=255)
    city: str = Field(min_length=1, max_length=100)
    state: str = Field(min_length=1, max_length=100)
    pincode: str = Field(max_length=10)
    country: str = Field(default="India", max_length=100)
    is_default: bool = False

    @field_validator("pincode")
    @classmethod
    def _pincode(cls, v: str) -> str:
        v = v.strip()
        if not PINCODE_RE.match(v):
            raise ValueError("Pincode must be exactly 6 digits")
        return v

    @field_validator("recipient_phone")
    @classmethod
    def _phone(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip().replace(" ", "")
        if not PHONE_RE.match(v):
            raise ValueError("Invalid phone number")
        return v


class AddressCreate(AddressBase):
    pass


class AddressUpdate(BaseModel):
    label: str | None = Field(default=None, max_length=50)
    recipient_name: str | None = Field(default=None, max_length=255)
    recipient_phone: str | None = Field(default=None, max_length=20)
    line1: str | None = Field(default=None, min_length=3, max_length=255)
    line2: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    pincode: str | None = Field(default=None, max_length=10)
    country: str | None = Field(default=None, max_length=100)
    is_default: bool | None = None

    @field_validator("pincode")
    @classmethod
    def _pincode(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not PINCODE_RE.match(v):
            raise ValueError("Pincode must be exactly 6 digits")
        return v


class AddressOut(ORMModel):
    id: int
    label: str
    recipient_name: str | None = None
    recipient_phone: str | None = None
    line1: str
    line2: str | None = None
    city: str
    state: str
    pincode: str
    country: str
    is_default: bool
    is_serviceable: bool


class ProfileUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    gstin: str | None = Field(default=None, max_length=15)
    phone: str | None = Field(default=None, max_length=20)

    @field_validator("gstin")
    @classmethod
    def _gstin(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip().upper()
        if not GSTIN_RE.match(v):
            raise ValueError("Invalid GSTIN format")
        return v

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip().replace(" ", "")
        if not PHONE_RE.match(v):
            raise ValueError("Invalid phone number")
        return v


class WhatsAppOptIn(BaseModel):
    opt_in: bool


class AdminUserSummary(ORMModel):
    id: int
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    auth_provider: str
    whatsapp_opt_in: bool
    order_count: int = 0
    total_spent: float = 0.0
