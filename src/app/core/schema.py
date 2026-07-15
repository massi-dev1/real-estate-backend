"""Pydantic base classes shared by every module.

- JSON is camelCase on the wire, snake_case in Python (single alias generator).
- Input schemas reject unknown fields (``extra="forbid"``) per §10.4.
- Output schemas are explicit ``*Out`` models built from ORM objects.
"""

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class BaseSchema(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class InputSchema(BaseSchema):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class OutSchema(BaseSchema):
    """Base for all ``*Out`` response models (serialized by alias)."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        serialize_by_alias=True,
    )
