"""Public attribution is not a private fetch capability."""
from urllib.parse import urlsplit


def public_source_reference(value: str | None) -> str | None:
    """Keep plain web references; omit authenticated or decorated fetch URLs.

    No parameter-name blacklist: an unknown query or fragment can be a credential.
    Removing it could also invent a different source, so omit the reference instead.
    """
    if not value:
        return None
    try:
        url = urlsplit(value)
        if (url.scheme not in {"http", "https"} or not url.hostname
                or url.username is not None or url.password is not None
                or url.query or url.fragment):
            return None
        return value
    except ValueError:
        return None
