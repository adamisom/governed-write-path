"""Search over the tenant's trusted policy text.

`Retriever` is the interface the proposer's search tool uses. A retriever is
built for one tenant, so the tenant is bound by code and the model can't pass
one. Two implementations:

- `BM25Retriever`, in-process and deterministic. It is the offline baseline and
  the one every test and offline eval uses.
- `S3VectorsRetriever`, for Amazon S3 Vectors with Titan Text Embeddings V2.
  UNTESTED: it has never run, because this machine has no AWS credentials. It is
  here to show where the managed backend plugs in.

Only policy chunks are indexed. Uploaded documents never are, so an injection in
one document can't be stored and retrieved by a later run.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Hit:
    chunk_id: str
    doc_id: str
    version: int
    score: float
    text: str


class Retriever(Protocol):
    tenant_id: str

    def search(self, query: str, k: int = 3) -> list[Hit]: ...


_STOP = frozenset(
    "a an and are as at be by for from has in is it of on or that the this to was were will with any all"
    " its not than then there these they which who what when where your our".split()
)


def tokenize(text: str) -> list[str]:
    text = re.sub(r"(?<=\d),(?=\d)", "", text.lower())  # 1,000.00 -> 1000.00
    text = re.sub(r"(?<=\d)\.00\b", "", text)  # 1000.00 -> 1000
    tokens = re.findall(r"[a-z0-9]+", text)
    out = []
    for tok in tokens:
        if tok in _STOP or len(tok) < 2:
            continue
        # A tiny stemmer: enough to match "invoices" to "invoice" and "chairs" to "chair".
        for suffix in ("ies", "es", "s"):
            if tok.endswith(suffix) and len(tok) > len(suffix) + 2 and not tok.isdigit():
                tok = tok[: -len(suffix)] + ("y" if suffix == "ies" else "")
                break
        out.append(tok)
    return out


class BM25Retriever:
    """Okapi BM25 over a tenant's policy chunks. Ties break on chunk id, so results are stable."""

    def __init__(self, tenant_id: str, chunks: list[dict], k1: float = 1.5, b: float = 0.75):
        self.tenant_id = tenant_id
        self.chunks = [c for c in chunks if c["tenant_id"] == tenant_id]
        self.k1, self.b = k1, b
        self.docs = [tokenize(c["text"]) for c in self.chunks]
        self.tf = [Counter(d) for d in self.docs]
        n = len(self.docs)
        self.avgdl = sum(len(d) for d in self.docs) / n if n else 0.0
        df: Counter[str] = Counter()
        for d in self.docs:
            df.update(set(d))
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, k: int = 3) -> list[Hit]:
        q = tokenize(query)
        scored = []
        for i, tf in enumerate(self.tf):
            dl = len(self.docs[i])
            score = 0.0
            for term in q:
                f = tf.get(term, 0)
                if not f:
                    continue
                score += self.idf[term] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            if score > 0:
                scored.append((round(score, 6), self.chunks[i]["chunk_id"], i))
        scored.sort(key=lambda s: (-s[0], s[1]))
        return [Hit(self.chunks[i]["chunk_id"], self.chunks[i]["doc_id"], self.chunks[i].get("version", 1), score,
                    self.chunks[i]["text"]) for score, _cid, i in scored[:k]]


class S3VectorsRetriever:
    """UNTESTED. Semantic search with Amazon S3 Vectors and Titan Text Embeddings V2.

    Expected setup (see infra/terraform/s3vectors.tf): one vector bucket, one index
    with 1024 dimensions and cosine distance, and each vector's metadata holding
    tenant_id, chunk_id, doc_id, version and text. The tenant filter is applied by
    this class, never by the model. Check the boto3 `s3vectors` API names against
    the current release before the first run; they have not been exercised here.
    """

    EMBED_MODEL = "amazon.titan-embed-text-v2:0"

    def __init__(self, tenant_id: str, vector_bucket: str, index_name: str, bedrock_runtime: Any = None,
                 s3vectors: Any = None, region: str = "us-east-1"):
        import boto3

        self.tenant_id = tenant_id
        self.bucket = vector_bucket
        self.index = index_name
        self.bedrock = bedrock_runtime or boto3.client("bedrock-runtime", region_name=region)
        self.vectors = s3vectors or boto3.client("s3vectors", region_name=region)

    def _embed(self, text: str) -> list[float]:
        resp = self.bedrock.invoke_model(
            modelId=self.EMBED_MODEL,
            body=json.dumps({"inputText": text, "dimensions": 1024, "normalize": True}),
        )
        return json.loads(resp["body"].read())["embedding"]

    def search(self, query: str, k: int = 3) -> list[Hit]:
        resp = self.vectors.query_vectors(
            vectorBucketName=self.bucket,
            indexName=self.index,
            queryVector={"float32": self._embed(query)},
            topK=k,
            filter={"tenant_id": self.tenant_id},
            returnMetadata=True,
            returnDistance=True,
        )
        hits = []
        for v in resp.get("vectors", []):
            md = v.get("metadata", {})
            if md.get("tenant_id") != self.tenant_id:  # defense in depth; the filter should already do this
                continue
            hits.append(Hit(md["chunk_id"], md["doc_id"], int(md.get("version", 1)), 1.0 - float(v.get("distance", 1.0)),
                            md.get("text", "")))
        return hits

    def index_chunks(self, chunks: list[dict]) -> None:
        """Embed and upload this tenant's chunks. One-time ingestion; also untested."""
        vectors = []
        for c in chunks:
            if c["tenant_id"] != self.tenant_id:
                continue
            vectors.append({
                "key": f"{self.tenant_id}#{c['chunk_id']}",
                "data": {"float32": self._embed(c["text"])},
                "metadata": {"tenant_id": c["tenant_id"], "chunk_id": c["chunk_id"], "doc_id": c["doc_id"],
                             "version": c.get("version", 1), "text": c["text"]},
            })
        for start in range(0, len(vectors), 100):
            self.vectors.put_vectors(vectorBucketName=self.bucket, indexName=self.index,
                                     vectors=vectors[start:start + 100])
