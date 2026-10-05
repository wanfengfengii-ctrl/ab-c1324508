"""Request/response models and field-level validation."""
from __future__ import annotations

import re
from typing import Dict, List

from pydantic import BaseModel, Field, field_validator
from pydantic_core import PydanticCustomError

from . import fixedpoint

_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")
_ID_PATTERN = r"[A-Za-z0-9._:\-]{1,128}"
_CHANNEL_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,64}$", re.ASCII)

MAX_CHANNELS = 16


def _validate_id(value: str, field: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise PydanticCustomError(
            "invalid_id",
            f"{field} must be 1-128 chars of [A-Za-z0-9._:-]",
        )
    return value


class ChannelPrescription(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    prescribed: str  # canonical positive decimal string

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _CHANNEL_RE.fullmatch(v):
            raise PydanticCustomError(
                "invalid_channel",
                "channel name must be 1-64 chars of [A-Za-z0-9._:-]",
            )
        return v

    @field_validator("prescribed")
    @classmethod
    def _dose(cls, v: str) -> str:
        try:
            fixedpoint.parse_dose(v, positive=True)
        except fixedpoint.DecimalError as exc:
            raise PydanticCustomError("invalid_dose", str(exc)) from exc
        return v


class CreateCourseRequest(BaseModel):
    channels: List[ChannelPrescription] = Field(min_length=1, max_length=MAX_CHANNELS)

    @field_validator("channels")
    @classmethod
    def _unique(cls, v: List[ChannelPrescription]) -> List[ChannelPrescription]:
        names = [c.name for c in v]
        if len(set(names)) != len(names):
            raise PydanticCustomError(
                "duplicate_channel", "channel names must be unique within a course"
            )
        return v


class DeliveryRequest(BaseModel):
    deliveryId: str = Field(min_length=1, max_length=128)
    expectedRevision: int = Field(ge=0)
    increments: Dict[str, str] = Field(min_length=1)

    @field_validator("deliveryId")
    @classmethod
    def _id(cls, v: str) -> str:
        return _validate_id(v, "deliveryId")

    @field_validator("increments")
    @classmethod
    def _increments(cls, v: Dict[str, str]) -> Dict[str, str]:
        if not v:
            raise PydanticCustomError(
                "empty_increments", "at least one channel increment is required"
            )
        for channel, dose in v.items():
            if not _CHANNEL_RE.fullmatch(channel):
                raise PydanticCustomError("invalid_channel", f"bad channel {channel!r}")
            try:
                fixedpoint.parse_dose(dose, positive=True)
            except fixedpoint.DecimalError as exc:
                raise PydanticCustomError(
                    "invalid_dose", f"{channel}: {exc}"
                ) from exc
        return v
