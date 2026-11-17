import re

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CreateCampaign(Input):
    name: str = Field(min_length=1, max_length=255)
    geo: str = Field(min_length=2, max_length=500)
    offer_id: int = Field(gt=0, strict=True)
    domain_id: int | None = Field(default=None, gt=0, strict=True)
    group_id: int | None = Field(default=None, gt=0, strict=True)
    source_id: int | None = Field(default=None, gt=0, strict=True)

    @field_validator("geo")
    @classmethod
    def geo_codes(cls, value):
        codes = list(dict.fromkeys(re.split(r"[,;\s]+", value.upper().strip())))
        if not codes or any(not re.fullmatch(r"[A-Z]{2}", code) for code in codes):
            raise ValueError("Укажите двухбуквенные коды стран: AU, MX, RO")
        return ",".join(codes)


class Revision(Input):
    revision: int = Field(ge=1, strict=True)


class AddOffer(Revision):
    offer_id: int = Field(gt=0, strict=True)


class PinOffer(Revision):
    pinned: bool = Field(strict=True)
    share: int | None = Field(default=None, ge=0, le=100, strict=True)


class DemoOffers(Input):
    offers: list[dict]
