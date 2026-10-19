"""Hybrid retrieval: BM25 over character 4-grams (+ optional BGE-M3 dense vectors), fused with RRF.

Character n-grams cope with Georgian morphology (suffixes change the word form, so word tokens miss).
One ``HybridIndex`` is built per node type (ProvisionVersion, Clause) or per document.
Dense vectors and the cross-encoder reranker are optional (config ``retrieval.dense`` / ``retrieval.reranker``) and
are skipped silently when ``sentence-transformers`` or the model files are not available.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import numpy as np

_WORD = re.compile(r"[ა-ჰA-Za-z0-9]+")


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def char_ngrams(text: str, n: int = 4) -> list[str]:
    """Padded character n-grams per word: 'მოდელი' -> '#მოდე', 'მოდელ', 'ოდელი', 'დელი#' ..."""
    out: list[str] = []
    for w in _WORD.findall(normalize(text)):
        w = f"#{w}#"
        if len(w) <= n:
            out.append(w)
        else:
            out.extend(w[i:i + n] for i in range(len(w) - n + 1))
    return out


def rrf(rankings: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda kv: -kv[1])


class DenseEncoder:
    """Lazy BGE-M3 wrapper. ``available`` is False when the model cannot be loaded."""

    def __init__(self, model_name: str):
        self.model_name = model_name
        self._model = None
        self.available = False
        try:
            from sentence_transformers import SentenceTransformer  # noqa: F401
            self.available = True
        except Exception:
            self.available = False

    def encode(self, texts: list[str]) -> np.ndarray:
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name)
        return np.asarray(self._model.encode(texts, normalize_embeddings=True, batch_size=16))


class HybridIndex:
    def __init__(self, items: list[dict], ngram: int = 4, rrf_k: int = 60, dense: DenseEncoder | None = None,
                 cache_dir: Path | None = None, name: str = "index"):
        """items: [{'id': str, 'text': str, ...}] (extra keys are kept and returned by ``get``)."""
        from rank_bm25 import BM25Okapi

        self.items = items
        self.by_id = {it["id"]: it for it in items}
        self.ngram, self.rrf_k = ngram, rrf_k
        self._tokens = [char_ngrams(it["text"], ngram) for it in items]
        self._bm25 = BM25Okapi(self._tokens) if items and any(self._tokens) else None
        self._dense_mat = None
        self._encoder = dense if (dense is not None and dense.available) else None
        if self._encoder is not None and items:
            self._dense_mat = self._load_or_encode(cache_dir, name)

    def _load_or_encode(self, cache_dir, name):
        key = hashlib.sha1("\n".join(f"{i['id']}\t{i['text']}" for i in self.items).encode()).hexdigest()[:16]
        path = Path(cache_dir) / f"{name}_{key}.npy" if cache_dir else None
        if path and path.exists():
            return np.load(path)
        try:
            mat = self._encoder.encode([i["text"] for i in self.items])
        except Exception:
            self._encoder = None
            return None
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            np.save(path, mat)
        return mat

    def get(self, item_id: str) -> dict | None:
        return self.by_id.get(item_id)

    def search(self, query: str, k: int = 10, allowed: set[str] | None = None) -> list[tuple[str, float]]:
        if not self.items:
            return []
        rankings: list[list[str]] = []
        if self._bm25 is not None:
            q = char_ngrams(query, self.ngram)
            if q:
                scores = self._bm25.get_scores(q)
                order = np.argsort(-scores)
                rankings.append([self.items[i]["id"] for i in order if scores[i] > 0 and
                                 (allowed is None or self.items[i]["id"] in allowed)][: max(k * 3, 30)])
        if self._dense_mat is not None and self._encoder is not None:
            try:
                qv = self._encoder.encode([query])[0]
                sims = self._dense_mat @ qv
                order = np.argsort(-sims)
                rankings.append([self.items[i]["id"] for i in order if allowed is None or self.items[i]["id"] in allowed][: max(k * 3, 30)])
            except Exception:
                pass
        if not rankings:
            return []
        return rrf([r for r in rankings if r], self.rrf_k)[:k]


def build_index(cfg, items: list[dict], name: str = "index") -> HybridIndex:
    rc = cfg["retrieval"]
    enc = DenseEncoder(rc["dense"]["model"]) if rc["dense"].get("enabled") else None
    return HybridIndex(items, rc.get("ngram", 4), rc.get("rrf_k", 60), enc, cfg.path("indexes"), name)


def maybe_rerank(cfg, query: str, hits: list[dict], keep: int) -> list[dict]:
    """Optional cross-encoder reranking (config retrieval.reranker.enabled); otherwise truncate."""
    rc = cfg["retrieval"]["reranker"]
    if not rc.get("enabled") or not hits:
        return hits[:keep]
    try:
        from sentence_transformers import CrossEncoder
        ce = CrossEncoder(rc["model"])
        scores = ce.predict([(query, h["text"]) for h in hits])
        order = np.argsort(-np.asarray(scores))
        return [hits[i] for i in order][:keep]
    except Exception:
        return hits[:keep]
