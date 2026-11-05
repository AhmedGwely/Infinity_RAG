"""Tests for the Streamlit chat API client and rendered citation details."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import requests
from streamlit.testing.v1 import AppTest

from frontend import streamlit_app


class FakeResponse:
    def __init__(
        self,
        payload: dict[str, Any],
        *,
        status_code: int = 200,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300

    def json(self) -> dict[str, Any]:
        return self._payload


def _api_response() -> dict[str, Any]:
    return {
        "answer": "The meter's documented setting is 230 V [S1].",
        "sources": [
            {
                "citation_id": "S1",
                "document_name": "Mk6Es Manual",
                "document_id": "manual-1",
                "passage": "The supported voltage setting is 230 V.",
                "pdf_pages": [12, 13],
                "printed_pages": [9],
                "source_pages": [
                    {"pdf_page": 12, "printed_page": None},
                    {"pdf_page": 13, "printed_page": 9},
                ],
                "section_number": "4.2",
                "section_title": "Voltage settings",
                "chunk_id": "chunk-1",
            }
        ],
    }


def test_request_answer_posts_question_and_returns_api_payload() -> None:
    seen: dict[str, Any] = {}

    class HttpClient:
        @staticmethod
        def post(url: str, **kwargs: Any) -> FakeResponse:
            seen.update(url=url, **kwargs)
            return FakeResponse(_api_response())

    payload = streamlit_app.request_answer(
        "What voltage setting is supported?",
        api_url="http://api.example/",
        http=HttpClient,
    )

    assert seen["url"] == "http://api.example/query"
    assert seen["json"] == {"question": "What voltage setting is supported?"}
    assert seen["timeout"] == streamlit_app.REQUEST_TIMEOUT_SECONDS
    assert payload == _api_response()


def test_request_answer_reports_api_connection_failure() -> None:
    class HttpClient:
        @staticmethod
        def post(url: str, **kwargs: Any) -> None:
            raise requests.ConnectionError("private connection details")

    try:
        streamlit_app.request_answer(
            "Question",
            api_url="http://api.example",
            http=HttpClient,
        )
    except streamlit_app.BackendRequestError as error:
        assert str(error) == (
            "The knowledge service is unavailable. Check that the API is running."
        )
        assert "private connection details" not in str(error)
    else:
        raise AssertionError("connection failure should be surfaced")


def test_request_answer_does_not_display_arbitrary_backend_error_details() -> None:
    class HttpClient:
        @staticmethod
        def post(url: str, **kwargs: Any) -> FakeResponse:
            return FakeResponse(
                {"detail": "provider key must not be exposed"},
                status_code=502,
            )

    try:
        streamlit_app.request_answer(
            "Question",
            api_url="http://api.example",
            http=HttpClient,
        )
    except streamlit_app.BackendRequestError as error:
        assert "Answer generation failed" in str(error)
        assert "provider key" not in str(error)
    else:
        raise AssertionError("HTTP failure should be surfaced")


def test_configured_api_url_uses_override_before_default(
    monkeypatch,
) -> None:
    from app.core.config import Settings

    monkeypatch.setenv("API_URL", "https://knowledge.example/api/")
    assert streamlit_app.configured_api_url(Settings()) == (
        "https://knowledge.example/api"
    )

    monkeypatch.delenv("API_URL")
    assert streamlit_app.configured_api_url(
        Settings(api_host="localhost", api_port=8123)
    ) == "http://localhost:8123"


def test_streamlit_chat_displays_answer_and_expandable_citation(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: FakeResponse({"status": "ok"}),
    )
    monkeypatch.setattr(
        requests,
        "post",
        lambda *args, **kwargs: FakeResponse(_api_response()),
    )

    app = AppTest.from_file(
        str(Path(__file__).resolve().parents[1] / "frontend" / "streamlit_app.py")
    ).run()
    assert not app.exception
    app.chat_input[0].set_value("What voltage setting is supported?").run()

    assert not app.exception
    assert any(
        "The meter's documented setting is 230 V [S1]." in item.value
        for item in app.markdown
    )
    assert any("S1" in item.label and "PDF page(s) 12, 13" in item.label for item in app.expander)
    assert any(
        "The supported voltage setting is 230 V." in item.value
        for item in app.markdown
    )

    app.button[0].click().run()
    assert not any(
        "The meter's documented setting is 230 V [S1]." in item.value
        for item in app.markdown
    )


def test_streamlit_chat_displays_connection_error(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: FakeResponse({"status": "ok"}),
    )

    def fail_post(*args: Any, **kwargs: Any) -> None:
        raise requests.ConnectionError("private connection details")

    monkeypatch.setattr(requests, "post", fail_post)

    app = AppTest.from_file(
        str(Path(__file__).resolve().parents[1] / "frontend" / "streamlit_app.py")
    ).run()
    app.chat_input[0].set_value("What voltage setting is supported?").run()

    assert not app.exception
    assert any(
        "knowledge service is unavailable" in item.value
        for item in app.error
    )
    assert all("private connection details" not in item.value for item in app.error)
