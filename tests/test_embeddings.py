"""Tests for local BGE-M3 embedding behavior."""

import sys
from types import ModuleType
from unittest.mock import Mock

import pytest

from app.core.config import Settings
from app.embeddings.embedding_service import BGEEmbeddingService, EMBEDDING_DIMENSION


def test_embedding_dimensions_and_normalization_are_consistent() -> None:
    """Document and query embeddings are normalized and have the same size."""
    raw_vector = [3.0, 4.0] + [0.0] * (EMBEDDING_DIMENSION - 2)
    model = Mock()
    model.encode.side_effect = lambda texts, **kwargs: [raw_vector for _ in texts]
    service = BGEEmbeddingService(model=model, model_name="BAAI/bge-m3")

    documents = service.embed_documents(["first chunk", "second chunk"])
    query = service.embed_query("search query")

    assert len(documents) == 2
    assert all(len(vector) == EMBEDDING_DIMENSION for vector in documents)
    assert documents[0] == pytest.approx([0.6, 0.8] + [0.0] * (EMBEDDING_DIMENSION - 2))
    assert query == pytest.approx(documents[0])
    assert model.encode.call_args_list[0].kwargs["normalize_embeddings"] is True
    assert model.encode.call_args_list[1].kwargs["normalize_embeddings"] is True


def test_empty_document_batch_does_not_load_model() -> None:
    """An empty batch is safe and does not initialize sentence-transformers."""
    service = BGEEmbeddingService(model_name="BAAI/bge-m3", settings=Settings())

    assert service.embed_documents([]) == []
    assert service._model is None


def test_embedding_rejects_bad_inputs_and_model_dimensions() -> None:
    model = Mock()
    model.encode.return_value = [[1.0, 2.0]]
    service = BGEEmbeddingService(model=model, model_name="BAAI/bge-m3")

    with pytest.raises(ValueError, match="non-empty strings"):
        service.embed_documents([""])
    with pytest.raises(ValueError, match="non-empty string"):
        service.embed_query(" ")
    with pytest.raises(ValueError, match="2 dimensions"):
        service.embed_documents(["text"])


def test_model_loading_is_lazy_and_selects_cuda_when_available(monkeypatch) -> None:
    """The configured device is supplied only when the model is first needed."""
    loaded: dict[str, object] = {}

    class FakeModel:
        def encode(self, texts, **kwargs):
            return [[1.0] + [0.0] * (EMBEDDING_DIMENSION - 1) for _ in texts]

    def create_model(model_name: str, *, device: str) -> FakeModel:
        loaded.update(model_name=model_name, device=device)
        return FakeModel()

    sentence_transformers = ModuleType("sentence_transformers")
    sentence_transformers.SentenceTransformer = create_model
    torch = ModuleType("torch")
    torch.cuda = type("Cuda", (), {"is_available": staticmethod(lambda: True)})()
    monkeypatch.setitem(sys.modules, "sentence_transformers", sentence_transformers)
    monkeypatch.setitem(sys.modules, "torch", torch)

    service = BGEEmbeddingService(settings=Settings())
    assert loaded == {}

    service.embed_query("question")

    assert loaded == {"model_name": "BAAI/bge-m3", "device": "cuda"}
