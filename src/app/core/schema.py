"""Pydantic base classes shared by every module.

- JSON is camelCase on the wire, snake_case in Python (single alias generator).
- Input schemas reject unknown fields (``extra="forbid"``) per §10.4.
- Output schemas are explicit ``*Out`` models built from ORM objects.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator
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


def reject_null_for(*fields: str) -> Any:
    """PATCH schemas use ``exclude_unset`` so omission means "leave alone" —
    but a NOT NULL column still can't accept an explicit ``null`` for a field
    the client did set. Returns a ``model_validator`` to attach on an
    ``InputSchema`` subclass for its non-nullable fields, e.g.::

        _reject_required_nulls = reject_null_for("slug", "isPublished")
    """

    def _check(self: BaseModel) -> BaseModel:
        nulled = {
            f
            for f in self.model_fields_set & set(fields)
            if getattr(self, f) is None
        }
        if nulled:
            raise ValueError(f"fields cannot be set to null: {sorted(nulled)}")
        return self

    return model_validator(mode="after")(_check)
