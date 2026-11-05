"""Streamlit chat interface for the Infinity Egypt R&D Knowledge Assistant."""

from __future__ import annotations

import os
from typing import Any

import requests

from app.core.config import Settings, load_settings


REQUEST_TIMEOUT_SECONDS = 90
HEALTH_TIMEOUT_SECONDS = 3


class BackendRequestError(RuntimeError):
    """A safe, user-presentable API connectivity or response error."""


def configured_api_url(settings: Settings | None = None) -> str:
    """Return API_URL override or the configured API host and port."""
    runtime_settings = settings or load_settings()
    api_url = os.getenv("API_URL") or (
        f"http://{runtime_settings.api_host}:{runtime_settings.api_port}"
    )
    return api_url.rstrip("/")


def request_answer(
    question: str,
    *,
    api_url: str,
    http: Any = requests,
) -> dict[str, Any]:
    """Send one question through the existing FastAPI /query endpoint."""
    try:
        response = http.post(
            f"{api_url.rstrip('/')}/query",
            json={"question": question},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.Timeout as error:
        raise BackendRequestError(
            "The knowledge service took too long to answer. Please try again."
        ) from error
    except requests.ConnectionError as error:
        raise BackendRequestError(
            "The knowledge service is unavailable. Check that the API is running."
        ) from error
    except requests.RequestException as error:
        raise BackendRequestError(
            "Could not send the question to the knowledge service."
        ) from error

    if not response.ok:
        safe_errors = {
            422: "The question was invalid. Please enter a non-empty question.",
            502: "Answer generation failed. Please try again later.",
            503: "Document retrieval is temporarily unavailable.",
        }
        detail = safe_errors.get(
            response.status_code,
            f"HTTP {response.status_code}",
        )
        raise BackendRequestError(f"The knowledge service returned an error: {detail}")

    try:
        payload = response.json()
    except ValueError as error:
        raise BackendRequestError(
            "The knowledge service returned an invalid response."
        ) from error
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("answer"), str)
        or not isinstance(payload.get("sources"), list)
    ):
        raise BackendRequestError(
            "The knowledge service returned an incomplete answer or citation response."
        )
    return payload


def check_backend(api_url: str, *, http: Any = requests) -> tuple[bool, str]:
    """Check the existing API health endpoint without exposing connection details."""
    try:
        response = http.get(
            f"{api_url.rstrip('/')}/health",
            timeout=HEALTH_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        return False, "Unavailable"
    if not response.ok:
        return False, f"HTTP {response.status_code}"
    try:
        healthy = response.json().get("status") == "ok"
    except (ValueError, AttributeError):
        healthy = False
    return (True, "Connected") if healthy else (False, "Unexpected health response")


def _render_sources(st: Any, sources: list[dict[str, Any]]) -> None:
    if not sources:
        st.caption("No source citations were returned for this answer.")
        return

    st.markdown("**Sources**")
    for source in sources:
        citation_id = source.get("citation_id", "Source")
        document_name = source.get("document_name") or "Unknown document"
        pdf_pages = source.get("pdf_pages")
        printed_pages = source.get("printed_pages")
        pdf_label = (
            ", ".join(str(page) for page in pdf_pages)
            if isinstance(pdf_pages, list) and pdf_pages
            else "unavailable"
        )
        printed_label = (
            ", ".join(str(page) for page in printed_pages)
            if isinstance(printed_pages, list) and printed_pages
            else "unavailable"
        )
        section_number = source.get("section_number")
        section_title = source.get("section_title")
        section = " ".join(
            str(value) for value in (section_number, section_title) if value
        )
        citation_label = (
            f"{citation_id} · {document_name} · PDF page(s) {pdf_label}"
        )
        if printed_label != "unavailable":
            citation_label += f" · printed page(s) {printed_label}"
        if section:
            citation_label += f" · {section}"

        with st.expander(citation_label):
            passage = source.get("passage")
            if isinstance(passage, str) and passage.strip():
                st.markdown(passage)
            else:
                st.info("The API did not return passage text for this citation.")
            if source.get("chunk_id"):
                st.caption(f"Chunk ID: {source['chunk_id']}")


def _render_assistant_message(st: Any, message: dict[str, Any]) -> None:
    with st.chat_message("assistant"):
        if message.get("error"):
            st.error(message["error"])
            return
        st.markdown(message["answer"])
        _render_sources(st, message["sources"])


def main() -> None:
    """Render the chat UI and proxy all questions through FastAPI."""
    try:
        import streamlit as st
    except ImportError as error:
        raise RuntimeError("Install project dependencies before starting Streamlit.") from error

    settings = load_settings()
    api_url = configured_api_url(settings)
    st.set_page_config(
        page_title="Infinity Egypt | R&D Knowledge Assistant",
        page_icon="⚡",
        layout="wide",
    )
    st.markdown(
        """
        <style>
        :root {
            --infinity-navy: #10243a;
            --infinity-teal: #159a9c;
        }
        .stApp {
            background: linear-gradient(180deg, #f5f8fb 0%, #ffffff 40%);
        }
        [data-testid="stSidebar"] {
            background: #10243a;
        }
        [data-testid="stSidebar"] * {
            color: #eef5f8;
        }
        [data-testid="stSidebar"] button {
            border-color: #159a9c;
        }
        h1, h2, h3 {
            color: #10243a;
        }
        [data-testid="stChatMessage"] {
            border: 1px solid #e4ebf0;
            border-radius: 12px;
            padding: 1rem;
        }
        [data-testid="stChatInput"] {
            border-color: #159a9c;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.title("Infinity Egypt | R&D Knowledge Assistant")
    st.caption(
        "Ask engineering questions and explore cited evidence from the indexed "
        "knowledge base."
    )

    with st.sidebar:
        st.header("Knowledge Assistant")
        st.write(
            "A research and development assistant for engineering documentation."
        )
        st.markdown("---")
        st.subheader("Backend connection")
        connected, status = check_backend(api_url)
        if connected:
            st.success(status)
        else:
            st.error(status)
        st.caption(f"Collection: `{settings.collection_name}`")
        st.caption("Document catalog is not exposed by the API.")
        if st.button("Clear conversation", use_container_width=True):
            st.session_state.messages = []
        st.caption("Questions are sent to the configured FastAPI service.")

    if "messages" not in st.session_state:
        st.session_state.messages = []

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            if message["role"] == "user":
                st.markdown(message["content"])
            elif message.get("error"):
                st.error(message["error"])
            else:
                st.markdown(message["answer"])
                _render_sources(st, message["sources"])

    question = st.chat_input("Ask an engineering question...")
    if question is None:
        return

    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        with st.spinner("Searching the knowledge base and preparing a cited answer..."):
            try:
                payload = request_answer(question, api_url=api_url)
            except BackendRequestError as error:
                message = {"role": "assistant", "error": str(error)}
                st.error(str(error))
            else:
                sources = [
                    source
                    for source in payload["sources"]
                    if isinstance(source, dict)
                ]
                message = {
                    "role": "assistant",
                    "answer": payload["answer"],
                    "sources": sources,
                }
                st.markdown(payload["answer"])
                _render_sources(st, sources)
    st.session_state.messages.append(message)


if __name__ == "__main__":
    main()
