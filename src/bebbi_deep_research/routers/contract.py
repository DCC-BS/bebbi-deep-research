"""API contract version shared with the pipe (`X-DR-API-Version`).

Raise `API_VERSION` when a request or response changes in a way an older pipe cannot handle (a removed or renamed
field, a new required field, a changed meaning). Additive response fields do not need a new version. The pipe sends
its version with every call; a different version is refused with a German message the pipe shows in the chat. Calls
without the header (n8n, scripts) are not checked.
"""

from fastapi import Request

from bebbi_deep_research.utils.errors import conflict, invalid_request

API_VERSION = 1
HEADER = "X-DR-API-Version"


def require_api_version(request: Request) -> None:
    """Refuse a caller that speaks another API version.

    Raises:
        ApiErrorException: 400 if the header is not a version number, 409 if its major version differs.
    """
    sent = request.headers.get(HEADER)
    if sent is None:
        return
    try:
        major = int(sent.strip().split(".")[0])
    except ValueError:
        raise invalid_request(f"{HEADER} must be a version number, got {sent[:20]!r}") from None
    if major != API_VERSION:
        raise conflict(
            f"Deep Research in Open WebUI (Schnittstelle {major}) passt nicht zur Version des Recherche-Dienstes "
            f"(Schnittstelle {API_VERSION}). Bitte wenden Sie sich an den Betrieb: Pipe und Dienst müssen "
            "gemeinsam aktualisiert werden."
        )
