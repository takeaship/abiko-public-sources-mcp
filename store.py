"""Versioned JSON documents and a small linear full-text search; no database."""
import json
import os
import re
import unicodedata
from pathlib import Path

ROOT = Path(os.environ.get("ABIKO_DATA", "data"))


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).casefold()


def save(record: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    target = ROOT / (record["id"] + ".json")
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    tmp.replace(target)


def load(document_id: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{24}", document_id):
        raise ValueError("Invalid document_id")
    return json.loads((ROOT / f"{document_id}.json").read_text())


def latest(backend: str) -> list[dict]:
    found = {}
    for path in ROOT.glob("*.json"):
        record = json.loads(path.read_text())
        if record["backend"] != backend:
            continue
        url = record["url"]
        if url not in found or record["retrieved_at"] > found[url]["retrieved_at"]:
            found[url] = record
    return list(found.values())


def citation(record: dict) -> dict:
    return {k: record[k] for k in ["id", "backend", "url", "final_url", "title",
                                  "retrieved_at", "temporal", "original", "native_success"]}


def search(backend: str, query: str, limit: int = 5) -> list[dict]:
    # Explicit AND over whitespace-separated terms. Japanese substrings need no tokenizer.
    terms = [normalize(t) for t in query.split() if normalize(t)]
    if not terms:
        raise ValueError("空の検索語は指定できません")
    hits = []
    for record in latest(backend):
        if not record["native_success"]:
            continue
        text = normalize(record["markdown"])
        if all(t in text for t in terms):
            pos = text.find(terms[0])
            score = sum(text.count(t) for t in terms)
            hits.append({**citation(record), "score": score,
                         "snippet": text[max(0, pos - 100): pos + 700],
                         "snippet_format": "NFKC/空白除去済み。原文の表配置はread_sourceで確認"})
    return sorted(hits, key=lambda r: (-r["score"], r["id"]))[:limit]
