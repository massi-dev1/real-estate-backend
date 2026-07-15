import pytest

from app.core.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    InvalidCursorError,
    clamp_limit,
    decode_cursor,
    encode_cursor,
)


def test_cursor_roundtrip() -> None:
    values = {"published_at": "2026-07-15T10:00:00Z", "id": "abc-123"}
    assert decode_cursor(encode_cursor(values)) == values


def test_decode_rejects_garbage() -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor("not-a-cursor!!!")


def test_decode_rejects_non_object_json() -> None:
    import base64

    cursor = base64.urlsafe_b64encode(b'["a", "b"]').decode()
    with pytest.raises(InvalidCursorError):
        decode_cursor(cursor)


def test_clamp_limit() -> None:
    assert clamp_limit(None) == DEFAULT_PAGE_SIZE
    assert clamp_limit(0) == 1
    assert clamp_limit(10_000) == MAX_PAGE_SIZE
    assert clamp_limit(50) == 50
