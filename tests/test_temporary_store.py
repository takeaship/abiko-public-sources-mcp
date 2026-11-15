"""Storage contracts, including actual process death and concurrent owners."""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pymupdf
import pytest
from test_evidence import record

import server
import store


@pytest.fixture
def documents(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(store.tempfile, "tempdir", str(tmp_path))
    documents = store.DocumentStore(history=tmp_path / "history.sqlite3", sweep_seconds=.01)
    monkeypatch.setattr(store, "_STORE", documents)
    yield documents
    documents.close()


def test_expiry_refresh_unknown_and_no_search_resurrection(documents, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(store.time, "monotonic", lambda: now[0])
    store.save(record("a" * 24, "検索対象"))
    now[0] += 7199
    assert store.search("crawl4ai", "検索対象")
    now[0] += 7199
    assert server.read_source("a" * 24)["text"] == "検索対象"
    now[0] += 7200
    result = server.read_source("a" * 24)
    assert result["status"] == "expired"
    assert result["source"]["original"]["sha256"] == "original-hash"
    assert result["refetch_url"].endswith("test.pdf")
    assert server.render_pdf_page("a" * 24, 1) == result
    assert server.read_source("b" * 24)["status"] == "unknown"
    assert not store.search("crawl4ai", "検索対象")
    assert not list(documents.root.glob("*.json"))


def test_idle_expiry_removes_original(documents):
    documents.ttl = .03
    value = record("a" * 24, "本文")
    original = documents.root / (value["id"] + ".pdf")
    original.write_bytes(b"pdf-original")
    value["original"]["path"] = str(original)
    store.save(value)
    deadline = time.monotonic() + 2
    while original.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert not original.exists()
    assert server.read_source(value["id"])["status"] == "expired"


def test_lru_size_includes_original_and_never_overwrites(documents, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(store.time, "monotonic", lambda: now[0])
    for key in "ab":
        store.save(record(key * 24, "本文"))
        now[0] += 1
    with pytest.raises(sqlite3.IntegrityError):
        store.save(record("a" * 24, "置換禁止"))
    assert store.load("a" * 24)["markdown"] == "本文"
    documents.max_bytes = sum(p.stat().st_size for p in documents.root.glob("*.json"))
    now[0] += 1
    store.load("a" * 24)
    original = documents.root / ("c" * 24 + ".pdf")
    original.write_bytes(b"x" * (documents.max_bytes + 1))
    store.save(record("c" * 24, "大きすぎる"))
    assert not list(documents.root.glob("*.json"))
    assert not original.exists()
    assert server.read_source("a" * 24)["status"] == "expired"


def test_size_evicts_least_recent_reference(documents, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(store.time, "monotonic", lambda: now[0])
    for key in "ab":
        store.save(record(key * 24, "本文"))
        now[0] += 1
    store.load("a" * 24)
    documents.max_bytes = max(p.stat().st_size for p in documents.root.glob("*.json"))
    documents.prune()
    assert store.load("a" * 24)
    assert server.read_source("b" * 24)["status"] == "expired"


def test_changes_use_previous_success_across_restarts(documents):
    first = record("a" * 24, "初版")
    store.save(first)
    assert first["content_change"]["status"] == "first"
    with ThreadPoolExecutor(2) as pool:
        # Separate stores sharing the DB, with separate temporary roots.
        siblings = list(pool.map(lambda _: store.DocumentStore(history=documents.history), range(2)))
    try:
        assert siblings[0].root != siblings[1].root != documents.root
        same = record("b" * 24, "同版")
        siblings[0].save(same)
        assert same["content_change"]["status"] == "unchanged"
        failed = record("c" * 24, "", success=False)
        siblings[1].save(failed)
        assert failed["content_change"]["status"] == "fetch_failed"
        changed = record("d" * 24, "新版")
        changed["original"]["sha256"] = "new-hash"
        siblings[1].save(changed)
        assert changed["content_change"] == {"status": "changed", "previous_id": same["id"], "previous_sha256": "original-hash"}
        with pytest.raises(store.MissingDocument) as exc:
            siblings[0].load(changed["id"])
        assert exc.value.result["status"] == "expired"
        assert documents.load(first["id"])["markdown"] == "初版"
    finally:
        for sibling in siblings:
            sibling.close()
    documents.close()
    restarted = store.DocumentStore(history=documents.history)
    try:
        with pytest.raises(store.MissingDocument) as exc:
            restarted.load(first["id"])
        assert exc.value.result["status"] == "expired"
    finally:
        restarted.close()


@pytest.mark.parametrize("pdf", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_fetch_no_envelopes_or_body_in_ledger_or_logs(documents, monkeypatch, capsys, caplog, pdf, failure):
    marker = "SECRET_BODY_SENTINEL"
    html = f"<html><title>資料</title><main>{marker}</main></html>"
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 50), marker)
    body = doc.tobytes()
    doc.close()

    def post(url, **kwargs):
        assert kwargs["json"]["crawler_config"]["params"]["verbose"] is False
        return httpx.Response(200, request=httpx.Request("POST", url), json={"results": [{
            "success": not failure, "status_code": 500 if failure else 200,
            "error_message": marker, "html": "" if pdf else html,
            "metadata": {"title": "資料"}, "markdown": {"raw_markdown": marker},
            "cleaned_html": f'<div class="pdf-page" data-page="1">{marker}</div>' if pdf else "",
        }]})

    def get(self, url):
        return httpx.Response(500 if failure else 200, content=body,
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(server.httpx, "post", post)
    monkeypatch.setattr(server.httpx.Client, "get", get)
    result = server.fetch_document("https://www.city.abiko.chiba.jp/test." + ("pdf" if pdf else "html"))
    stored = store.load(result["id"])
    assert "native_response" not in stored and "native_request" not in stored
    if failure:
        assert result["error"] == "source_HTTP_500"
    assert result["content_change"]["status"] == ("fetch_failed" if failure else "first")
    if not failure:
        assert server.read_source(result["id"])["text"] == marker
        if pdf:
            assert server.render_pdf_page(result["id"], 1).data
    with documents.connect() as db:
        citation = db.execute("SELECT citation FROM fetched_versions").fetchone()[0]
    assert marker not in citation
    assert "pages" not in citation and "path" not in citation
    assert marker not in capsys.readouterr().err + caplog.text


def test_explicit_archive_survives_close_and_limits(tmp_path):
    data, history = tmp_path / "archive", tmp_path / "history.sqlite3"
    archive = store.DocumentStore(data=data, history=history, ttl=0, max_bytes=0)
    archive.save(record("a" * 24, "保存証跡"))
    archive.close()
    reopened = store.DocumentStore(data=data, history=history)
    try:
        assert reopened.load("a" * 24)["markdown"] == "保存証跡"
    finally:
        reopened.close()


@pytest.mark.parametrize("ending", ["normal", "error", "sigkill"])
def test_process_lifecycle_and_orphan_recovery(tmp_path, ending):
    env = {**os.environ, "TMPDIR": str(tmp_path), "ABIKO_HISTORY": str(tmp_path / "history.sqlite3")}
    env.pop("ABIKO_DATA", None)
    code = '''
import json, signal, time
import server, store
s = store.current()
print(s.root, flush=True)
if ENDING == "normal":
    server.mcp.run = lambda **kw: None
elif ENDING == "error":
    def fail(**kw): raise RuntimeError("fixture failure")
    server.mcp.run = fail
else:
    def wait(**kw):
        print("ready", flush=True)
        while True: time.sleep(.1)
    server.mcp.run = wait
server.main()
'''.replace("ENDING", repr(ending))
    process = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        root = Path(process.stdout.readline().strip())
        assert root.is_dir()
        if ending == "sigkill":
            assert process.stdout.readline().strip() == "ready"
            process.kill()
        _, stderr = process.communicate(timeout=15)
        assert "SECRET_BODY" not in stderr
        if ending == "sigkill":
            assert root.exists()
            # A new interpreter must remove the orphan, but leave unrelated data alone.
            unrelated = tmp_path / "unrelated.txt"
            unrelated.write_text("keep")
            subprocess.run([sys.executable, "-c", "import store; store.current().close()"],
                           env=env, check=True, capture_output=True, timeout=15)
            assert unrelated.read_text() == "keep"
        assert not root.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_live_process_not_scavenged_and_shared_ledger(tmp_path):
    env = {**os.environ, "TMPDIR": str(tmp_path), "ABIKO_HISTORY": str(tmp_path / "history.sqlite3")}
    env.pop("ABIKO_DATA", None)
    code = '''
import json, time
import store
s = store.current()
r = json.loads(INPUT)
s.save(r)
print(s.root, flush=True)
time.sleep(30)
'''
    processes = []
    try:
        for key in "ab":
            child = subprocess.Popen([sys.executable, "-c", code.replace("INPUT", repr(json.dumps(record(key * 24, key))))],
                                     env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            processes.append(child)
            root = Path(child.stdout.readline().strip())
            assert (root / (key * 24 + ".json")).exists()
        first_root = Path(tmp_path / f"abiko-public-sources-{os.getuid()}")
        assert len(list(first_root.glob("process-*/*.json"))) == 2
        with sqlite3.connect(tmp_path / "history.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM fetched_versions").fetchone()[0] == 2
    finally:
        for process in processes:
            process.kill()
            process.communicate(timeout=10)
        subprocess.run([sys.executable, "-c", "import store; store.current().close()"],
                       env=env, check=True, capture_output=True, timeout=15)


def test_expired_latest_failure_never_exposes_older_success(documents, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(store.time, "monotonic", lambda: now[0])
    store.save(record("a" * 24, "検索対象"))
    now[0] += 1
    store.save(record("b" * 24, "", timestamp="2026-11-14T03:00:01+09:00", success=False))
    now[0] += 1
    store.load("a" * 24)
    now[0] += 7199
    assert not store.search("crawl4ai", "検索対象")
    assert store.load("a" * 24)["markdown"] == "検索対象"


def test_transport_failure_and_immediately_evicted_discovery(documents, monkeypatch):
    def fail(*args, **kwargs):
        raise httpx.ConnectError("SECRET_RESPONSE_BODY")
    monkeypatch.setattr(server.httpx, "post", fail)
    result = server.fetch_document("https://www.city.abiko.chiba.jp/test.html")
    assert result["error"] == "backend_ConnectError"
    assert result["original"]["error"] == "backend_ValueError"
    assert result["content_change"]["status"] == "fetch_failed"
    def unauthorized(url, **kwargs):
        return httpx.Response(401, json={"error": "SECRET_RESPONSE_BODY"},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(server.httpx, "post", unauthorized)
    unauthorized_result = server.fetch_document("https://www.city.abiko.chiba.jp/test.html")
    assert unauthorized_result["error"] == "backend_HTTP_401"
    documents.max_bytes = 0
    result = server.discover_links("https://www.city.abiko.chiba.jp/test.html")
    assert result["source"]["status"] == "expired"
    assert result["links"] == []
    assert b"SECRET_RESPONSE_BODY" not in documents.history.read_bytes()


def test_real_stdio_default_mode_tools_and_cleanup(tmp_path):
    import asyncio
    from datetime import timedelta

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def exercise():
        env = {**os.environ, "TMPDIR": str(tmp_path), "ABIKO_HISTORY": str(tmp_path / "history.sqlite3")}
        env.pop("ABIKO_DATA", None)
        code = '''
import httpx, server

def post(url, **kwargs):
    return httpx.Response(200, request=httpx.Request("POST", url), json={"results":[{
        "success":True, "html":"<title>Fixture</title>STDIO_BODY_SENTINEL",
        "markdown":{"raw_markdown":"STDIO_BODY_SENTINEL"},
        "links":{"internal":[{"href":"https://www.city.abiko.chiba.jp/test.html","text":"Test"}]}
    }]})
server.httpx.post = post
server.main()
'''
        params = StdioServerParameters(command=sys.executable, args=["-c", code], env=env)
        with (tmp_path / "stderr.log").open("w") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write), ClientSession(read, write) as session:
                await session.initialize()

                async def call(tool, args):
                    result = await session.call_tool(tool, args, read_timeout_seconds=timedelta(seconds=15))
                    assert not result.isError
                    return result.structuredContent or json.loads(result.content[0].text)

                fetched = await call("fetch_document", {"url": "https://www.city.abiko.chiba.jp/test.html"})
                root = Path(fetched["original"]["path"]).parent
                assert root.is_relative_to(tmp_path)
                assert root.is_dir()
                assert (await call("read_source", {"document_id": fetched["id"]}))["text"] == "STDIO_BODY_SENTINEL"
                assert (await call("search_documents", {"query": "STDIO_BODY_SENTINEL"}))["hits"]
                assert (await call("discover_links", {"url": fetched["url"]}))["links"]
                assert (await call("render_pdf_page", {"document_id": "f" * 24, "page": 1}))["status"] == "unknown"
        assert not root.exists()
        assert "STDIO_BODY_SENTINEL" not in (tmp_path / "stderr.log").read_text()
        assert b"STDIO_BODY_SENTINEL" not in (tmp_path / "history.sqlite3").read_bytes()
    asyncio.run(exercise())


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_signal_exits_real_stdio_with_stdin_still_open(tmp_path, sig):
    env = {**os.environ, "TMPDIR": str(tmp_path), "ABIKO_HISTORY": str(tmp_path / "history.sqlite3")}
    env.pop("ABIKO_DATA", None)
    child = subprocess.Popen([sys.executable, "server.py"], env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        child.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "signal-fixture", "version": "1"}}}) + "\n")
        child.stdin.flush()
        assert json.loads(child.stdout.readline())["id"] == 1
        root = next(tmp_path.glob("abiko-public-sources-*/process-*"))
        # Keep the transport input open; EOF must not be needed for termination.
        child.send_signal(sig)
        child.wait(timeout=5)
        assert child.returncode == 128 + sig
        assert not root.exists()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


def test_empty_archive_env_means_temporary(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(store.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("ABIKO_DATA", "")
    monkeypatch.setenv("ABIKO_HISTORY", str(tmp_path / "history.sqlite3"))
    monkeypatch.setattr(store, "_STORE", None)
    documents = store.current()
    try:
        assert not documents.persistent
        assert documents.root.is_relative_to(tmp_path)
    finally:
        documents.close()


def test_failed_archive_write_never_publishes_partial_json(tmp_path, monkeypatch):
    archive = store.DocumentStore(data=tmp_path / "archive", history=tmp_path / "history.sqlite3")
    try:
        archive.save(record("a" * 24, "既存の証跡"))
        dump = store.json.dump

        def interrupted(record, out, **kwargs):
            out.write('{"partial":')
            raise OSError("fixture disk failure")

        monkeypatch.setattr(store.json, "dump", interrupted)
        with pytest.raises(OSError):
            archive.save(record("b" * 24, "失敗する新版"))
        monkeypatch.setattr(store.json, "dump", dump)
        assert [r["id"] for r in archive.latest("crawl4ai")] == ["a" * 24]
        assert not list(archive.root.glob("*.tmp"))
        with archive.connect() as db:
            assert db.execute("SELECT count(*) FROM fetched_versions").fetchone()[0] == 1
    finally:
        archive.close()


def test_readonly_distributed_index_replacement_preserves_default_history(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(store.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "user-state"))
    monkeypatch.delenv("ABIKO_HISTORY", raising=False)
    index = tmp_path / "distribution" / "abiko.sqlite3"
    index.parent.mkdir()
    with sqlite3.connect(index) as db:
        db.execute("CREATE TABLE distribution_fixture (version INTEGER)")
        db.execute("INSERT INTO distribution_fixture VALUES (1)")
    original = index.read_bytes()
    index.chmod(0o444)
    index.parent.chmod(0o555)
    monkeypatch.setenv("ABIKO_INDEX", str(index))
    documents = store.DocumentStore()
    try:
        assert documents.history == tmp_path / "user-state/abiko-public-sources-mcp/history.sqlite3"
        documents.save(record("a" * 24, "初版"))
        assert index.read_bytes() == original
    finally:
        documents.close()
        index.parent.chmod(0o755)
    replacement = index.with_suffix(".replacement")
    replacement.write_bytes(b"replacement index: PR1 must not open this file")
    replacement.replace(index)
    restarted = store.DocumentStore()
    try:
        with pytest.raises(store.MissingDocument) as exc:
            restarted.load("a" * 24)
        assert exc.value.result["status"] == "expired"
        same = record("b" * 24, "再取得")
        restarted.save(same)
        assert same["content_change"]["status"] == "unchanged"
        assert index.read_bytes() == b"replacement index: PR1 must not open this file"
    finally:
        restarted.close()


@pytest.mark.parametrize("alias", ["same_path", "symlink", "hardlink"])
def test_history_rejects_alias_of_distributed_index(tmp_path, monkeypatch, alias):
    index = tmp_path / "abiko.sqlite3"
    index.write_bytes(b"distributed index must remain untouched")
    history = index
    if alias != "same_path":
        history = tmp_path / "history.sqlite3"
        if alias == "symlink":
            history.symlink_to(index)
        else:
            os.link(index, history)
    monkeypatch.setenv("ABIKO_INDEX", str(index))
    with pytest.raises(ValueError, match="別ファイル"):
        store.DocumentStore(history=history)
    assert index.read_bytes() == b"distributed index must remain untouched"


def test_default_history_home_and_explicit_override(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user-home")
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("ABIKO_INDEX", str(tmp_path / "not-installed.sqlite3"))
    monkeypatch.delenv("ABIKO_HISTORY", raising=False)
    documents = store.DocumentStore(data=tmp_path / "evidence")
    try:
        assert documents.history == tmp_path / "user-home/.local/state/abiko-public-sources-mcp/history.sqlite3"
        assert documents.history.is_file()
        assert not (tmp_path / "not-installed.sqlite3").exists()
    finally:
        documents.close()
    explicit = tmp_path / "custom.sqlite3"
    monkeypatch.setenv("ABIKO_HISTORY", str(explicit))
    documents = store.DocumentStore(data=tmp_path / "evidence")
    try:
        assert documents.history == explicit
        assert documents.history.is_file()
        assert not (tmp_path / "not-installed.sqlite3").exists()
    finally:
        documents.close()


@pytest.mark.parametrize("trigger", ["signal", "normal_close"])
def test_repeated_signal_during_cleanup_does_not_deadlock(tmp_path, trigger):
    env = {**os.environ, "TMPDIR": str(tmp_path), "ABIKO_HISTORY": str(tmp_path / "history.sqlite3")}
    env.pop("ABIKO_DATA", None)
    code = """
import os, signal, server, store
remove = store.shutil.rmtree
def repeated_signal(path):
    os.kill(os.getpid(), signal.SIGINT)
    remove(path)
store.shutil.rmtree = repeated_signal
server.main()
"""
    child = subprocess.Popen([sys.executable, "-c", code], env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        child.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "repeated-signal-fixture", "version": "1"}}}) + "\n")
        child.stdin.flush()
        assert json.loads(child.stdout.readline())["id"] == 1
        root = next(tmp_path.glob("abiko-public-sources-*/process-*"))
        if trigger == "signal":
            child.send_signal(signal.SIGTERM)
        else:
            child.stdin.close()
            child.stdin = None
        child.wait(timeout=5)
        assert child.returncode in {128 + signal.SIGINT, 128 + signal.SIGTERM}
        assert not root.exists()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


def test_failed_history_write_cleans_new_original_and_preserves_existing(documents, monkeypatch):
    documents.save(record("a" * 24, "既存の証跡"))

    def post(url, **kwargs):
        return httpx.Response(200, request=httpx.Request("POST", url), json={"results": [{
            "success": True, "html": "<main>new source</main>",
            "markdown": {"raw_markdown": "new source"}}]})

    def fail(record):
        raise sqlite3.OperationalError("fixture: history database is locked")

    monkeypatch.setattr(server.httpx, "post", post)
    monkeypatch.setattr(store, "save", fail)
    with pytest.raises(sqlite3.OperationalError):
        server.fetch_document("https://www.city.abiko.chiba.jp/new.html")
    assert not list(documents.root.glob("*.html"))
    assert list(documents.root.glob("*.json")) == [documents.root / ("a" * 24 + ".json")]
    assert documents.load("a" * 24)["markdown"] == "既存の証跡"


def test_partial_original_write_is_removed(documents, monkeypatch):
    from contextlib import contextmanager

    def post(url, **kwargs):
        return httpx.Response(200, request=httpx.Request("POST", url), json={"results": [{
            "success": True, "html": "<main>new source</main>",
            "markdown": {"raw_markdown": "new source"}}]})

    open_file = Path.open

    @contextmanager
    def interrupted_open(path, mode="r", *args, **kwargs):
        with open_file(path, mode, *args, **kwargs) as out:
            if path.suffix == ".html" and mode == "xb":
                class BrokenWriter:
                    def write(self, body):
                        out.write(body[:5])
                        raise OSError("fixture: disk full")
                yield BrokenWriter()
            else:
                yield out

    monkeypatch.setattr(server.httpx, "post", post)
    monkeypatch.setattr(Path, "open", interrupted_open)
    with pytest.raises(OSError, match="disk full"):
        server.fetch_document("https://www.city.abiko.chiba.jp/new.html")
    assert not list(documents.root.glob("*.html"))
    assert not list(documents.root.glob("*.json"))
