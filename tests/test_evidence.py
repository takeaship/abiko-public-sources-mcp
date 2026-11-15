"""Regressions for evidence separation and Japanese PDF search."""
import pytest

import server
import store


def record(id, text, timestamp="2026-11-14T03:00:00+09:00", success=True):
    return {"id": id, "backend": "crawl4ai", "url": "https://www.city.abiko.chiba.jp/test.pdf",
            "final_url": "https://www.city.abiko.chiba.jp/test.pdf", "title": "採決結果",
            "retrieved_at": timestamp, "native_success": success, "markdown": text,
            "native_pages": [], "temporal": {"stage": "voting_result"},
            "original": {"success": True, "sha256": "original-hash", "page_count": 1,
                         "pages": [{"page": 1, "text": "これは別経路で抽出した原本です"}]}}


def test_japanese_pdf_unicode_spacing_and_and_query(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path)
    store.save(record("a" * 24, "議 案 第 ８ 号 一般会計補正予算 全 ２３ ０"))
    assert store.search("crawl4ai", "議案第8号 一般会計")[0]["id"] == "a" * 24
    assert not store.search("crawl4ai", "議案第8号 可決していない")
    assert not store.search("firecrawl", "議案第8号")


def test_new_failure_does_not_silently_serve_old_success(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path)
    store.save(record("a" * 24, "議案第8号"))
    store.save(record("b" * 24, "", timestamp="2026-11-14T03:00:01+09:00", success=False))
    assert not store.search("crawl4ai", "議案第8号")
    assert store.load("a" * 24)["markdown"] == "議案第8号"


def test_native_text_cannot_be_replaced_by_original_pdf_extraction(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path)
    store.save(record("a" * 24, ""))
    assert server.read_source("a" * 24)["text"] == ""
    with pytest.raises(ValueError, match="ページ境界"):
        server.read_source("a" * 24, page=1)
    source = server.read_source("a" * 24, page=1, representation="original_pdf")
    assert source["text"] == "これは別経路で抽出した原本です"
    assert source["source_url"].endswith("#page=1")
    assert source["original"]["sha256"] == "original-hash"
    assert source["temporal"]["stage"] == "voting_result"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1/a", "https://www.city.abiko.chiba.jp.evil.example/a", "https://user:pass@www.city.abiko.chiba.jp/a", "https://www.city.abiko.chiba.jp/cgi-bin/formmail/formmail.cgi", "https://ssp.kaigiroku.net/tenant/another-city/a"])
def test_discovery_is_limited_to_public_abiko_sources(url):
    with pytest.raises(ValueError):
        server.allowed_url(url)


def test_version_id_cannot_escape_store():
    with pytest.raises(ValueError):
        store.load("../../.env")
