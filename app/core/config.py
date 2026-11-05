"""Centralized environment-based application settings."""

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Runtime configuration loaded from environment variables and .env."""

    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    openai_api_key: str | None = None
    gemini_api_key: str | None = None
    llm_provider: str = "ollama"
    llm_model: str = "qwen3:8b"
    gemini_model: str = "gemini-3.8-flash"
    ollama_base_url: str = "http://localhost:11434"
    embedding_model: str = "BAAI/bge-m3"
    collection_name: str = "rd_engineering"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    log_level: str = "INFO"
    incoming_documents_dir: Path = Path("documents/incoming")
    processed_documents_dir: Path = Path("documents/processed")
    ingestion_chunk_size: int = 1600
    ingestion_chunk_overlap: int = 200
    docling_offline: bool = True


def load_settings() -> Settings:
    """Load settings, reading a local .env file when python-dotenv is installed."""
    try:
        from dotenv import load_dotenv
    except ImportError as error:
        raise RuntimeError("Install project dependencies before loading settings.") from error

    load_dotenv()
    return Settings(
        qdrant_url=os.getenv("QDRANT_URL", Settings.qdrant_url),
        qdrant_api_key=os.getenv("QDRANT_API_KEY") or None,
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
        llm_provider=os.getenv("LLM_PROVIDER", Settings.llm_provider),
        llm_model=os.getenv("LLM_MODEL", Settings.llm_model),
        gemini_model=os.getenv("GEMINI_MODEL", Settings.gemini_model),
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", Settings.ollama_base_url),
        embedding_model=os.getenv("EMBEDDING_MODEL", Settings.embedding_model),
        collection_name=os.getenv("COLLECTION_NAME", Settings.collection_name),
        api_host=os.getenv("API_HOST", Settings.api_host),
        api_port=int(os.getenv("API_PORT", str(Settings.api_port))),
        log_level=os.getenv("LOG_LEVEL", Settings.log_level),
        incoming_documents_dir=Path(
            os.getenv("INCOMING_DOCUMENTS_DIR", str(Settings.incoming_documents_dir))
        ),
        processed_documents_dir=Path(
            os.getenv("PROCESSED_DOCUMENTS_DIR", str(Settings.processed_documents_dir))
        ),
        ingestion_chunk_size=int(
            os.getenv("INGESTION_CHUNK_SIZE", str(Settings.ingestion_chunk_size))
        ),
        ingestion_chunk_overlap=int(
            os.getenv("INGESTION_CHUNK_OVERLAP", str(Settings.ingestion_chunk_overlap))
        ),
        docling_offline=os.getenv("DOCLING_OFFLINE", "true").lower() in {"1", "true", "yes"},
    )
