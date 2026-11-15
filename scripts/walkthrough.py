"""Replay a citizen's multi-hop investigation, plus explicit original-image checks."""
import asyncio
import base64
import hashlib
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


async def main():
    os.chdir(ROOT)
    run = ROOT / sys.argv[1]
    out = run / "walkthrough"
    out.mkdir(exist_ok=False)
    report = {"scenario": "トップページから2024年6月議会の岩井康議員の質問要項へ到達し、大綱1を確認", "backends": {}}
    params = StdioServerParameters(command=sys.executable, args=["server.py"], cwd=str(ROOT),
                                   env={**os.environ, "ABIKO_DATA": str(run / "data"), "ABIKO_HISTORY": str(run / "history.sqlite3")})
    with (out / "toolcalls.jsonl").open("w") as log:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()

            async def call(tool, arguments):
                result = await session.call_tool(tool, arguments, read_timeout_seconds=timedelta(seconds=180))
                if result.isError:
                    value = {"error": [c.text for c in result.content if c.type == "text"]}
                elif tool == "render_pdf_page":
                    img = next(c for c in result.content if c.type == "image")
                    body = base64.b64decode(img.data)
                    path = out / f"{arguments['document_id']}-{arguments['page']}.png"
                    path.write_bytes(body)
                    value = {"path": str(path.relative_to(ROOT)), "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
                else:
                    value = result.structuredContent or json.loads(next(c.text for c in result.content if c.type == "text"))
                log.write(json.dumps({"tool": tool, "arguments": arguments, "result": value}, ensure_ascii=False) + "\n")
                log.flush()
                return value

            backend = "crawl4ai"
            route = []

            async def follow(url, contains, choose):
                response = await call("discover_links", {"url": url, "contains": contains, "limit": 100})
                chosen = next(a["url"] for a in response["links"] if choose(a))
                route.append({"from": url, "to": chosen, "method": response["method"]})
                await asyncio.sleep(1)
                return chosen

            url = await follow("https://www.city.abiko.chiba.jp/", "/shisei/", lambda a: a["url"].endswith("/shisei/index.html"))
            url = await follow(url, "/shigikai/", lambda a: a["url"].endswith("/shigikai/index.html"))
            url = await follow(url, "/teirei_rinji/", lambda a: a["url"].endswith("/teirei_rinji/index.html"))
            url = await follow(url, "r6dai2kai6gatsu", lambda a: a["url"].endswith("/index.html"))
            url = await follow(url, "ippanshitsumon", lambda a: a["url"].endswith(".html"))
            url = await follow(url, ".pdf", lambda a: a["url"].endswith(".pdf"))
            fetch = await call("fetch_document", {"url": url})
            hits = await call("search_documents", {"query": "補聴器 補助制度"})
            assert any(h["url"] == url for h in hits["hits"])
            source = await call("read_source", {"document_id": fetch["id"], "page": 2})
            if "error" in source:
                source = await call("read_source", {"document_id": fetch["id"], "page": 2, "representation": "original_pdf"})
            assert "補聴器" in source["text"]
            row = {"route": route, "fetch": fetch, "source": source,
                   "answer": "岩井康議員の大綱1は補聴器購入の補助制度を、という質問。要項なので市の回答や実施済みの根拠にはならない。"}
            records = [json.loads(p.read_text()) for p in (run / "data").glob("*.json")]
            row["images"] = []
            for suffix, page in [("saiketukekkaR6.6.pdf", 1), ("20141023-121014.pdf", 2)]:
                doc = max((r for r in records if r["backend"] == backend and r["url"].endswith(suffix)), key=lambda r: r["retrieved_at"])
                rendered = await call("render_pdf_page", {"document_id": doc["id"], "page": page})
                row["images"].append({"url": doc["url"], "page": page, **rendered})
            row["external_links"] = await call("discover_links", {"url": "https://ssp.kaigiroku.net/tenant/abiko/SpTop.html", "limit": 100})
            row["unverified_future_query"] = await call("search_documents", {"query": "2034年度 新計画"})
            report["backends"][backend] = row
    (out / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(out / "results.json")


if __name__ == "__main__":
    asyncio.run(main())
