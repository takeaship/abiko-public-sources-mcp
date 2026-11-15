"""Process-local documents, with a durable metadata-only version ledger."""
import atexit
import fcntl
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import unicodedata
from contextlib import closing, contextmanager
from pathlib import Path


class MissingDocument(Exception):
    def __init__(self, result):
        self.result = result
        super().__init__(result["status"])


def validate_id(document_id):
    if not re.fullmatch(r"[a-f0-9]{24}", document_id):
        raise ValueError("Invalid document_id")


class DocumentStore:
    def __init__(self, *, data=None, history=None, ttl=7200, max_bytes=500 * 1024 * 1024,
                 sweep_seconds=30):
        self.persistent = data is not None
        self.ttl, self.max_bytes = ttl, max_bytes
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.access = {}
        self.latest_ids = {}
        state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
        self.history = Path(history or os.environ.get("ABIKO_HISTORY") or
                            state_home / "abiko-public-sources-mcp/history.sqlite3")
        distributed_index = Path(os.environ.get("ABIKO_INDEX") or "index/abiko.sqlite3")
        if (self.history.resolve() == distributed_index.resolve() or
                self.history.exists() and distributed_index.exists() and self.history.samefile(distributed_index)):
            raise ValueError("ABIKO_HISTORY と ABIKO_INDEX は別ファイルにしてください")
        self.history.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS fetched_versions (
                id TEXT PRIMARY KEY, backend TEXT NOT NULL, url TEXT NOT NULL,
                retrieved_at TEXT NOT NULL, sha256 TEXT, success INTEGER NOT NULL,
                citation TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS fetched_versions_url ON fetched_versions(backend, url, retrieved_at)")
        self.owner = None
        if self.persistent:
            self.root = Path(data)
            self.root.mkdir(parents=True, exist_ok=True)
        else:
            # Lifetime flock avoids PID reuse and protects live sibling processes.
            namespace = Path(tempfile.gettempdir()) / f"abiko-public-sources-{os.getuid()}"
            namespace.mkdir(mode=0o700, exist_ok=True)
            if namespace.is_symlink() or namespace.stat().st_uid != os.getuid() or namespace.stat().st_mode & 0o077:
                raise ValueError("Unsafe temporary namespace")
            with (namespace / ".lock").open("a") as guard:
                fcntl.flock(guard, fcntl.LOCK_EX)
                for path in namespace.glob("process-*"):
                    if path.is_symlink() or not path.is_dir():
                        continue
                    with (path / ".owner").open("a") as owner:
                        try:
                            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            continue
                        shutil.rmtree(path)
                self.root = Path(tempfile.mkdtemp(prefix="process-", dir=namespace))
                self.owner = (self.root / ".owner").open("a")
                fcntl.flock(self.owner, fcntl.LOCK_EX)
        self.thread = threading.Thread(target=self._sweep, args=(sweep_seconds,), daemon=True)
        if not self.persistent:
            self.thread.start()
        atexit.register(self.close)

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.history, timeout=30)) as db, db:
            yield db

    def close(self):
        self.stop.set()
        with self.lock:
            if self.owner is not None:
                # Serialize deletion against another process's startup scavenger.
                with (self.root.parent / ".lock").open("a") as guard:
                    fcntl.flock(guard, fcntl.LOCK_EX)
                    shutil.rmtree(self.root)
                    self.owner.close()
                    self.owner = None
        atexit.unregister(self.close)

    def _sweep(self, interval):
        while not self.stop.wait(interval):
            self.prune()

    def _delete(self, document_id):
        for path in self.root.glob(document_id + ".*"):
            path.unlink()
        self.access.pop(document_id, None)

    def prune(self):
        with self.lock:
            if self.persistent or self.stop.is_set():
                return
            now = time.monotonic()
            for document_id, accessed in list(self.access.items()):
                if now - accessed >= self.ttl:
                    self._delete(document_id)
            sizes = dict.fromkeys(self.access, 0)
            for path in self.root.iterdir():
                if path.stem in sizes:
                    sizes[path.stem] += path.stat().st_size
            total = sum(sizes.values())
            for document_id in sorted(self.access, key=self.access.get):
                if total <= self.max_bytes:
                    break
                total -= sizes[document_id]
                self._delete(document_id)

    def save(self, record):
        validate_id(record["id"])
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("""SELECT id, sha256 FROM fetched_versions
                WHERE backend=? AND url=? AND success=1 AND sha256 IS NOT NULL
                ORDER BY retrieved_at DESC, rowid DESC LIMIT 1""",
                (record["backend"], record["url"])).fetchone()
            original = record["original"]
            digest = original.get("sha256") if original["success"] else None
            status = ("fetch_failed" if not record["native_success"] else
                      "unavailable" if digest is None else
                      "first" if previous is None else
                      "unchanged" if digest == previous[1] else "changed")
            record["content_change"] = {"status": status,
                "previous_id": previous[0] if previous else None,
                "previous_sha256": previous[1] if previous else None}
            provenance = {key: record[key] for key in (
                "id", "backend", "url", "final_url", "title", "retrieved_at", "temporal", "native_success")}
            provenance.update(error=record.get("error"), original={
                key: original.get(key) for key in ("success", "sha256", "retrieved_at", "error")})
            # Explicit allowlist: never copy pages, source paths or backend envelopes.
            db.execute("INSERT INTO fetched_versions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record["id"], record["backend"], record["url"], record["retrieved_at"], digest,
                 record["native_success"], json.dumps(provenance, ensure_ascii=False)))
            target = self.root / (record["id"] + ".json")
            temporary = target.with_suffix(".tmp")
            out = temporary.open("x")
            try:
                with out:
                    json.dump(record, out, ensure_ascii=False, indent=2)
                # Publish a complete file without ever replacing an existing version.
                os.link(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            self.access[record["id"]] = time.monotonic()
            key = record["backend"], record["url"]
            version = record["retrieved_at"], record["id"]
            self.latest_ids[key] = max(version, self.latest_ids.get(key, version))
        self.prune()

    def load(self, document_id, *, touch=True):
        validate_id(document_id)
        with self.lock:
            self.prune()
            path = self.root / f"{document_id}.json"
            if path.exists():
                record = json.loads(path.read_text())
                if touch:
                    self.access[document_id] = time.monotonic()
                return record
            with self.connect() as db:
                row = db.execute("SELECT citation FROM fetched_versions WHERE id=?", (document_id,)).fetchone()
            if row:
                source = json.loads(row[0])
                raise MissingDocument({"status": "expired", "message": "期限切れです。URLをfetch_documentで再取得してください。",
                                       "source": source, "refetch_url": source["url"]})
            raise MissingDocument({"status": "unknown", "message": "未知のdocument_idです。", "document_id": document_id})

    def latest(self, backend):
        with self.lock:
            self.prune()
            found = {}
            for path in self.root.glob("*.json"):
                record = json.loads(path.read_text())
                if record["backend"] != backend:
                    continue
                url = record["url"]
                if not self.persistent and self.latest_ids.get((backend, url), (None, None))[1] != record["id"]:
                    continue
                if url not in found or record["retrieved_at"] > found[url]["retrieved_at"]:
                    found[url] = record
            return list(found.values())


_STORE = None
_INIT_LOCK = threading.Lock()


def current():
    global _STORE
    with _INIT_LOCK:
        if _STORE is None:
            _STORE = DocumentStore(data=os.environ.get("ABIKO_DATA") or None)
    return _STORE


def save(record):
    current().save(record)


def load(document_id):
    return current().load(document_id)


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).casefold()


def citation(record: dict) -> dict:
    return {k: record[k] for k in ["id", "backend", "url", "final_url", "title",
                                  "retrieved_at", "temporal", "original", "native_success"]}


def search(backend: str, query: str, limit: int = 5) -> list[dict]:
    with current().lock:
        terms = [normalize(t) for t in query.split() if normalize(t)]
        if not terms:
            raise ValueError("空の検索語は指定できません")
        hits = []
        for record in current().latest(backend):
            if not record["native_success"]:
                continue
            text = normalize(record["markdown"])
            if all(t in text for t in terms):
                pos = text.find(terms[0])
                score = sum(text.count(t) for t in terms)
                hits.append({**citation(record), "score": score,
                             "snippet": text[max(0, pos - 100): pos + 700],
                             "snippet_format": "NFKC/空白除去済み。原文の表配置はread_sourceで確認"})
        result = sorted(hits, key=lambda r: (-r["score"], r["id"]))[:limit]
        for hit in result:
            current().access[hit["id"]] = time.monotonic()
        return result
