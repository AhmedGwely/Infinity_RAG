"""Local API launch entry point."""

from app.api.routes import create_app
from app.core.config import load_settings
from app.core.logging_config import configure_logging


def create_application():
    """Factory used by Uvicorn, keeping optional server dependencies lazy."""
    configure_logging()
    return create_app()


def main() -> None:
    """Load settings and serve the FastAPI application locally."""
    settings = load_settings()
    configure_logging(settings.log_level)
    try:
        import uvicorn
    except ImportError as error:
        raise RuntimeError("Install project dependencies before starting the API.") from error

    uvicorn.run(
        "main:create_application",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        reload=True,
    )


if __name__ == "__main__":
    main()
