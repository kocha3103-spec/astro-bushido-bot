"""
Поиск по астрологическим материалам через облачную базу Trychroma.
"""

import os
import chromadb

CHROMA_API_KEY = os.environ.get("CHROMA_API_KEY", "ck-HYQNZTMoyxxGQbEZQq6YaXC82Z6DDzrmAcwHjoTQ7x1k")
CHROMA_TENANT = "801f8452-7477-4d36-851d-2ec2e9b61522"
CHROMA_DATABASE = "astrolog"
COLLECTION_NAME = "file_upload"
TOP_K = 4

_collection = None

def _get_collection():
    global _collection
    if _collection is None:
        client = chromadb.CloudClient(
            api_key=CHROMA_API_KEY,
            tenant=CHROMA_TENANT,
            database=CHROMA_DATABASE,
        )
        _collection = client.get_collection(name=COLLECTION_NAME)
    return _collection

def retrieve(query: str, n_results: int = TOP_K) -> str:
    """Ищет релевантные куски из материалов и возвращает текст для промпта."""
    try:
        collection = _get_collection()
        results = collection.query(query_texts=[query], n_results=n_results)
        docs = results["documents"][0]
        metas = results["metadatas"][0]
        if not docs:
            return ""
        parts = []
        for doc, meta in zip(docs, metas):
            source = meta.get("source", meta.get("source_type", ""))
            parts.append(f"[{source}]\n{doc}")
        return "\n\n---\n\n".join(parts)
    except Exception as e:
        # Если база недоступна — бот работает без RAG, не падает
        return ""

def build_query(lunation_type: str, sign: str, house: int,
                planet_names: list = None) -> str:
    """Составляет поисковый запрос из данных лунации."""
    type_name = "новолуние" if lunation_type == "НЛ" else "полнолуние"
    parts = [f"{type_name} {sign}", f"луна {sign}", f"{house} дом луна"]
    if planet_names:
        parts.extend([p for p in planet_names[:3] if not p.startswith("_")])
    return " ".join(parts)
