from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING, Callable, Iterable
from itertools import islice
from threading import Lock
from uuid import uuid4

from src.workflow.resources import embedding_batch_size, memory_slot

if TYPE_CHECKING:
    from fastembed import TextEmbedding


MODEL_ID = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MODEL_DIMENSIONS = 384
MODEL_REVISION = "qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q"
CHUNK_VERSION = "char-token-320-overlap-48-v1"
_MODEL: TextEmbedding | None = None
_MODEL_LOCK = Lock()


def _workspace_model_dir() -> Path:
    path = Path(os.getenv("KNOWLEDGE_EMBEDDING_CACHE", "data/models/fastembed")).resolve()
    if path.drive.upper() != "E:":
        raise RuntimeError("EMBEDDING_CACHE_MUST_BE_ON_E: local model cache must remain on E:\\")
    path.mkdir(parents=True, exist_ok=True)
    hf_home = path.parent / "huggingface"
    hf_home.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(hf_home)
    os.environ["HUGGINGFACE_HUB_CACHE"] = str(hf_home / "hub")
    os.environ["HF_HUB_CACHE"] = str(hf_home / "hub")
    return path


def _asset_manifest(model_dir: Path) -> dict[str, str]:
    result = {}
    for path in sorted(model_dir.rglob("*")):
        if not path.is_file() or path.name == "model-manifest.json":
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        result[str(path.relative_to(model_dir))] = digest.hexdigest()
    return result


def _load_embedding_model():
    try:
        from fastembed import TextEmbedding
    except ImportError as exc:
        raise RuntimeError("EMBEDDING_MODEL_UNAVAILABLE: FastEmbed is not installed") from exc
    model_dir = _workspace_model_dir()
    registered = {item["model"]: item for item in TextEmbedding.list_supported_models()}
    if MODEL_ID not in registered or int(registered[MODEL_ID].get("dim", 0)) != MODEL_DIMENSIONS:
        raise RuntimeError("EMBEDDING_MODEL_UNAVAILABLE: expected multilingual 384-d FastEmbed model is not registered")
    try:
        model = TextEmbedding(model_name=MODEL_ID, cache_dir=str(model_dir), threads=2, cuda=False)
    except Exception as exc:
        raise RuntimeError(f"EMBEDDING_MODEL_UNAVAILABLE: local model could not be loaded from E: ({type(exc).__name__})") from exc
    actual = _asset_manifest(model_dir)
    if not actual:
        raise RuntimeError("EMBEDDING_MODEL_UNAVAILABLE: model loaded without verifiable local model files")
    manifest_path = model_dir / "model-manifest.json"
    if manifest_path.exists():
        expected = json.loads(manifest_path.read_text(encoding="utf-8"))
        if expected.get("model_id") != MODEL_ID or expected.get("model_revision") != MODEL_REVISION or expected.get("files") != actual:
            raise RuntimeError("EMBEDDING_MODEL_CHECKSUM_MISMATCH: cached model files differ from the verified local manifest")
    else:
        manifest_path.write_text(json.dumps({"model_id": MODEL_ID, "model_revision": MODEL_REVISION,
            "dimensions": MODEL_DIMENSIONS, "files": actual}, ensure_ascii=False, indent=2), encoding="utf-8")
    return model


def get_embedding_model() -> "LocalEmbeddingModel":
    global _MODEL
    if _MODEL is None:
        with _MODEL_LOCK:
            if _MODEL is None:
                with memory_slot("embedding", reserve_mb=640):
                    _MODEL = _load_embedding_model()
    return LocalEmbeddingModel(_MODEL)


class LocalEmbeddingModel:
    def __init__(self, model: TextEmbedding):
        self.model = model

    def embed_documents(self, texts: Iterable[str]) -> list[list[float]]:
        result = []
        iterator = iter(texts)
        while batch := list(islice(iterator, embedding_batch_size())):
            with memory_slot("embedding"):
                for vector in self.model.embed(batch, batch_size=len(batch), parallel=None):
                    values = vector.tolist()
                    if len(values) != MODEL_DIMENSIONS:
                        raise RuntimeError("EMBEDDING_DIMENSION_MISMATCH")
                    result.append(values)
        return result

    def embed_query(self, text: str) -> list[float]:
        vectors = self.embed_documents([text])
        return vectors[0]


_TOKEN_RE = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9]+(?:['’._-][A-Za-z0-9]+)*|[^\s]")


def _iter_document_chunks(title: str, body: str, *, max_tokens: int = 320, overlap: int = 48):
    if not 0 <= overlap < max_tokens:
        raise ValueError("chunk overlap must be smaller than max_tokens")
    text = (str(title or "").strip() + "\n" + str(body or "").strip()).strip()
    tokens = iter(_TOKEN_RE.finditer(text))
    window = []
    chunk_index = 0
    while added := list(islice(tokens, max_tokens - len(window))):
        window.extend(added)
        start_char, end_char = window[0].start(), window[-1].end()
        yield {
            "chunk_index": chunk_index,
            "content": text[start_char:end_char],
            "char_start": start_char,
            "char_end": end_char,
            "token_count": len(window),
        }
        if len(window) < max_tokens:
            break
        window = window[-overlap:] if overlap else []
        chunk_index += 1


def chunk_document(title: str, body: str, *, max_tokens: int = 320, overlap: int = 48) -> list[dict[str, int | str]]:
    return list(_iter_document_chunks(title, body, max_tokens=max_tokens, overlap=overlap))


def _prepared_document_chunks(document: dict, worker):
    pieces = iter(_iter_document_chunks(document.get("title", ""), document.get("body", "")))
    version = str(document["content_hash"])
    namespace = str(document.get("account_namespace") or "local")
    record_id = str(document["record_id"])
    while batch := list(islice(pieces, 16)):
        vectors = worker.embed_documents(piece["content"] for piece in batch)
        if len(vectors) != len(batch):
            raise RuntimeError("EMBEDDING_BATCH_INCOMPLETE")
        for piece, vector in zip(batch, vectors):
            key = f"{namespace}\0{record_id}\0{version}\0{piece['chunk_index']}\0{CHUNK_VERSION}"
            yield {**piece, "chunk_id": hashlib.sha256(key.encode()).hexdigest(),
                   "document_version": version, "batch_id": uuid4().hex, "embedding": vector}


def prepare_chunks(documents: Iterable[dict], *, embedder=None) -> list[tuple[dict, list[dict]]]:
    worker = embedder or get_embedding_model()
    return [(document, list(_prepared_document_chunks(document, worker))) for document in documents]


def index_pending_documents(
    store,
    *,
    batch_size: int = 32,
    embedder=None,
    progress_callback: Callable[[dict[str, int]], None] | None = None,
    account_namespace: str | None = None,
) -> dict[str, int]:
    indexed_documents = 0
    batch_size = max(1, min(32, int(batch_size)))
    indexed_chunks = 0
    pending = store.pending_documents(limit=batch_size, **({'account_namespace': account_namespace} if account_namespace is not None else {}))
    if not pending:
        return {"indexed_documents": 0, "indexed_chunks": 0}
    worker = embedder or get_embedding_model()
    attempted_versions = set()
    while pending:
        versions = {(str(doc.get("account_namespace")), str(doc["record_id"]), str(doc["content_hash"])) for doc in pending}
        if versions & attempted_versions:
            raise RuntimeError("KNOWLEDGE_INDEX_NOT_READY: no progress committing document versions")
        attempted_versions.update(versions)
        for document in pending:
            chunks = _prepared_document_chunks(document, worker)
            if not getattr(store, "streaming_chunks", False):
                chunks = list(chunks)
            with memory_slot("embedding"):
                indexed_chunks += store.upsert_chunks(document["record_id"], chunks, account_namespace=document["account_namespace"])
            indexed_documents += 1
        if progress_callback:
            progress_callback({
                **store.index_progress(),
                "indexed_documents_this_run": indexed_documents,
                "indexed_chunks_this_run": indexed_chunks,
            })
        pending = store.pending_documents(limit=batch_size, **({'account_namespace': account_namespace} if account_namespace is not None else {}))
    return {"indexed_documents": indexed_documents, "indexed_chunks": indexed_chunks}
