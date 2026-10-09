"""Console entry point: `bebbi-deep-research` starts the service with uvicorn."""

import os

import uvicorn


def main() -> None:
    uvicorn.run("service.app:create_app", factory=True, host=os.environ.get("RA_HOST", "0.0.0.0"),
                port=int(os.environ.get("RA_PORT", "8088")), log_config=None, timeout_graceful_shutdown=20,
                proxy_headers=False)


if __name__ == "__main__":
    main()
