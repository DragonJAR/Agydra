"""Shared module-facing access to the central message catalog."""
import i18n


def module_text(key: str, default: str, **kwargs: object) -> str:
    """Use the central catalog with an English default and safe interpolation.

    Locale selection and fail-open interpolation belong solely to i18n.t.
    """
    return i18n.t(key, default=default, **kwargs)
