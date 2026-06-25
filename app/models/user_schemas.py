from pydantic import BaseModel, Field


class UpdateUserDto(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    phone_number: str | None = None
    profile_image: str | None = None
    preferences: dict | None = None
    # ISO 3166-1 alpha-2 country code. Changing this re-routes the user to
    # a different payment gateway on their NEXT checkout — existing
    # subscriptions stay on whichever gateway they originally signed up on.
    country_code: str | None = Field(default=None, min_length=2, max_length=2)


class UserStatsOut(BaseModel):
    documents_count: int
    signatures_count: int
    data_fields_count: int
    last_activity: str | None
