"""Locale negotiation for i18n JSONB fields (§8.1).

Content fields are stored as ``{"ar": ..., "fr": ..., "en": ...}``. The public
API returns one negotiated value per field: an explicit ``?locale=`` wins,
then ``Accept-Language``, then the fallback chain — never a hole where another
locale had content.
"""

from collections.abc import Mapping

SUPPORTED_LOCALES: tuple[str, ...] = ("ar", "fr", "en")
DEFAULT_LOCALE = "fr"
# Tried after the requested locale; ends with every supported locale so a
# field that has *any* translation always resolves.
FALLBACK_CHAIN: tuple[str, ...] = ("fr", "en", "ar")


def negotiate_locale(explicit: str | None, accept_language: str | None) -> str:
    """Pick the response locale: explicit query param > Accept-Language > default."""
    if explicit and explicit in SUPPORTED_LOCALES:
        return explicit
    if accept_language:
        for part in accept_language.split(","):
            code = part.split(";")[0].strip().lower()[:2]
            if code in SUPPORTED_LOCALES:
                return code
    return DEFAULT_LOCALE


def pick_localized(values: Mapping[str, str] | None, locale: str) -> str | None:
    """Resolve one i18n mapping to a single string using the fallback chain."""
    if not values:
        return None
    for candidate in (locale, *FALLBACK_CHAIN):
        text = values.get(candidate)
        if text:
            return text
    return None
