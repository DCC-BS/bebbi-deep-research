"""Bearer-token check shared by the API routers."""

import hmac

from dependency_injector.wiring import Provide, inject
from fastapi import Depends, Request

from bebbi_deep_research.container import Container
from bebbi_deep_research.utils.app_config import AppConfig
from bebbi_deep_research.utils.errors import unauthorized


@inject
def require_token(request: Request, config: AppConfig = Depends(Provide[Container.config])) -> None:
    """Reject requests without the service token (compared as bytes, in constant time).

    Raises:
        ApiErrorException: 401 if the token is missing or wrong.
    """
    sent = request.headers.get("authorization", "").encode()
    if not config.token or not hmac.compare_digest(sent, f"Bearer {config.token}".encode()):
        raise unauthorized()
