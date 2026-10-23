#!/usr/bin/env python3
"""
ChromaStore.py

Chroma-backed incident store. Same interface as incident_store.py
(add_incident / query_similar) so callers don't need to change - only
the storage backend does.

Important: we do NOT let Chroma generate embeddings. Our vectors already
come from metrics_to_vector() (a deterministic, versioned 8-dim
regression signature) - Chroma is used purely as a vector index + doc
store on top of vectors we compute ourselves. Passing embeddings=[...]
explicitly on add() and query() bypasses Chroma's default embedding
function entirely.

Persistence: uses a local on-disk PersistentClient (./chroma_data) so
data survives across script runs without a separate server process.
Swap to chromadb.HttpClient(...) later if you want to run Chroma as a
standalone server instead.

Chroma metadata values must be str/int/float/bool - dicts and lists
aren't supported directly, so baseline_metrics/feature_metrics are
stored as JSON strings and tags as a comma-joined string, then decoded
back on read.
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import chromadb

CHROMA_DATA_DIR = Path(__file__).resolve().parent / "chroma_data"
COLLECTION_NAME = "regression_incidents"

_client = None
_collection = None


def _get_collection():
    global _client, _collection
    if _collection is None:
        _client = chromadb.PersistentClient(path=str(CHROMA_DATA_DIR))
        # cosine space explicitly - Chroma's default is squared L2, which
        # is NOT what we want given how metrics_to_vector was designed
        # (shape of change matters more than raw magnitude - see earlier
        # discussion on why cosine was chosen over Euclidean distance).
        _collection = _client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def _next_id(collection) -> int:
    return collection.count() + 1


def add_incident(vector: List[float], vector_schema_version: int, root_cause: str,
                  fix: str, baseline_metrics: Optional[Dict] = None,
                  feature_metrics: Optional[Dict] = None, tags: Optional[List[str]] = None) -> Dict:
    collection = _get_collection()
    incident_id = _next_id(collection)

    metadata = {
        "vector_schema_version": vector_schema_version,
        "root_cause": root_cause,
        "fix": fix,
        "baseline_metrics_json": json.dumps(baseline_metrics) if baseline_metrics else "",
        "feature_metrics_json": json.dumps(feature_metrics) if feature_metrics else "",
        "tags": ",".join(tags) if tags else "",
    }

    collection.add(
        ids=[str(incident_id)],
        embeddings=[vector],
        metadatas=[metadata],
        documents=[root_cause],  # lets Chroma's own text search/inspection tools work too
    )

    return {"id": incident_id, "vector": vector, **metadata}


def _decode_metadata(meta: dict) -> dict:
    return {
        "vector_schema_version": meta.get("vector_schema_version"),
        "root_cause": meta.get("root_cause"),
        "fix": meta.get("fix"),
        "baseline_metrics": json.loads(meta["baseline_metrics_json"]) if meta.get("baseline_metrics_json") else None,
        "feature_metrics": json.loads(meta["feature_metrics_json"]) if meta.get("feature_metrics_json") else None,
        "tags": meta["tags"].split(",") if meta.get("tags") else [],
    }


def query_similar(query_vector: List[float], top_k: int = 3,
                   min_similarity: float = 0.5) -> List[Tuple[float, Dict]]:
    """
    Returns up to top_k (similarity, incident) pairs, highest similarity
    first, excluding anything below min_similarity.

    Chroma's cosine space returns DISTANCE (0 = identical), not
    similarity, so we convert: similarity = 1 - distance.

    Deliberately returns an empty list rather than forcing a match if
    nothing clears the threshold - same reasoning as the flat-file
    version: an unforced "no similar incidents" is a valid result.
    """
    collection = _get_collection()
    if collection.count() == 0:
        return []

    n = min(top_k * 3, collection.count())  # over-fetch a bit before threshold filtering
    results = collection.query(query_embeddings=[query_vector], n_results=n)

    ids = results["ids"][0]
    distances = results["distances"][0]
    metadatas = results["metadatas"][0]

    scored = []
    for incident_id, distance, meta in zip(ids, distances, metadatas):
        similarity = 1 - distance
        if similarity >= min_similarity:
            incident = {"id": int(incident_id), **_decode_metadata(meta)}
            scored.append((similarity, incident))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[:top_k]


def wipe():
    """Deletes the collection entirely (used by --wipe in SeedIncidents.py)."""
    global _collection
    client = chromadb.PersistentClient(path=str(CHROMA_DATA_DIR))
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass
    _collection = None