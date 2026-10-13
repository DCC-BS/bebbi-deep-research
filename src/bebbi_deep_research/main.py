"""Console entry point: `bebbi-deep-research` starts the service with uvicorn."""

import os

import uvicorn


def main() -> None:
    """Run the service (host and port from RA_HOST and RA_PORT)."""
    uvicorn.run(
        "bebbi_deep_research.app:create_app",
        factory=True,
        host=os.getenv("RA_HOST", "0.0.0.0"),
        port=int(os.getenv("RA_PORT", "8088")),
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=20,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
