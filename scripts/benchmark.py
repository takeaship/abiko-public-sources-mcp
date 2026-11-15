"""Exercise the actual stdio MCP, not in-process tool functions or provider SDKs."""
import argparse
import asyncio
import gzip
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from store import normalize

CONTAINER = "abiko-bench-crawl4ai-1"


def now():
    return datetime.now(ZoneInfo("Asia/Tokyo")).isoformat()


def cpu_usage():
    text = subprocess.check_output(["docker", "exec", CONTAINER, "cat", "/sys/fs/cgroup/cpu.stat"], text=True)
    return {CONTAINER: int(dict(line.split() for line in text.splitlines())["usage_usec"])}


def sample_stats(target, stop):
    with target.open("w") as out:
        while not stop.is_set():
            output = subprocess.check_output(["docker", "stats", "--no-stream", "--format", "{{json .}}", CONTAINER], text=True)
            for line in output.splitlines():
                out.write(json.dumps({"at": now(), **json.loads(line)}) + "\n")
            out.flush()
            stop.wait(1)


async def evaluate(args):
    os.chdir(ROOT)
    run = ROOT / args.output
    run.mkdir(parents=True, exist_ok=False)
    data = run / "data"
    env = {**os.environ, "ABIKO_DATA": str(data), "ABIKO_HISTORY": str(run / "history.sqlite3")}
    corpus = json.loads((ROOT / "evaluation/corpus.json").read_text())
    questions = json.loads((ROOT / args.questions).read_text())
    (run / "corpus.json").write_text(json.dumps(corpus, ensure_ascii=False, indent=2))
    (run / "questions.json").write_text(json.dumps(questions, ensure_ascii=False, indent=2))
    if args.only_docs:
        corpus = [r for r in corpus if r["id"] in args.only_docs]
        questions = [q for q in questions if q["doc"] in args.only_docs]
    if args.smoke:
        corpus = [r for r in corpus if r["id"] in {"move", "votes", "council"}]
        questions = [q for q in questions if q["doc"] in {"move", "votes"}]
    by_id = {r["id"]: r for r in json.loads((ROOT / "evaluation/corpus.json").read_text())}
    backend = "crawl4ai"
    report = {"started_at": now(), "backends": [backend], "repeats": args.repeats,
              "question_file": args.questions, "fetches": [], "questions": [], "discovery": []}
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "server.py")], env=env, cwd=str(ROOT))
    with (run / "toolcalls.jsonl").open("w") as log:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            report["tools"] = [t.name for t in (await session.list_tools()).tools]

            async def call(name, arguments):
                begin = time.monotonic()
                result = await session.call_tool(name, arguments, read_timeout_seconds=timedelta(seconds=180))
                if result.isError:
                    raise RuntimeError(f"MCP {name}: {result.content}")
                value = result.structuredContent
                if value is None:
                    value = json.loads(next(c.text for c in result.content if c.type == "text"))
                log.write(json.dumps({"at": now(), "tool": name, "arguments": arguments,
                                      "seconds": time.monotonic() - begin, "result": value}, ensure_ascii=False) + "\n")
                log.flush()
                return value

            for repeat in range(args.repeats):
                before = cpu_usage()
                stop = threading.Event()
                sampler = threading.Thread(target=sample_stats, args=(run / f"{backend}-{repeat}-stats.jsonl", stop))
                sampler.start()
                try:
                    fetched = {}
                    for doc in corpus:
                        result = await call("fetch_document", {"url": doc["url"]})
                        fetched[doc["id"]] = result
                        report["fetches"].append({"repeat": repeat, "doc": doc["id"], "group": doc["group"], **result})
                        print(backend, repeat, doc["id"], result["native_success"], result["native_characters"], round(result["native_seconds"], 2), flush=True)
                        await asyncio.sleep(1)

                    # One-hop link discovery; expected target URLs are used only for scoring.
                    routes = {"council": ["schedule", "bills", "questions", "votes"],
                              "budget": ["budget_pdf"], "plan_detail": ["plan_pdf"],
                              "minutes_gateway": ["minutes_external"]}
                    for seed, targets in routes.items():
                        if seed not in fetched:
                            continue
                        result = await call("discover_links", {"url": by_id[seed]["url"], "limit": 100})
                        urls = [a["url"] for a in result["links"]]
                        target_results = {t: by_id[t]["url"] in urls or by_id[t]["url"].replace("https:", "http:") in urls for t in targets}
                        report["discovery"].append({"repeat": repeat, "backend": backend, "seed": seed, "targets": target_results, "result": result})
                        await asyncio.sleep(1)

                    for q in questions:
                        expected = by_id[q["doc"]]
                        result = await call("search_documents", {"query": q["query"]})
                        hits = result["hits"]
                        ranks = [i + 1 for i, h in enumerate(hits) if h["url"] == expected["url"]]
                        # Read the hit via MCP when found. Oracle read of a missed expected doc is diagnostic only.
                        doc_id = hits[ranks[0] - 1]["id"] if ranks else fetched[q["doc"]]["id"]
                        offset, texts = 0, []
                        while True:
                            source = await call("read_source", {"document_id": doc_id, "offset": offset, "length": 20000})
                            texts.append(source["text"])
                            if source["next_offset"] is None:
                                break
                            offset = source["next_offset"]
                        text = "\n".join(texts)
                        missing = [n for n in q["needles"] if normalize(n) not in normalize(text)]
                        row = {"repeat": repeat, "backend": backend, "question": q["id"],
                               "rank": ranks[0] if ranks else None, "read_mode": "search_hit" if ranks else "oracle_diagnostic",
                               "missing_needles": missing, "native_coverage": not missing,
                               "source": {k: source[k] for k in ["url", "retrieved_at", "temporal", "original"]}}
                        if q["page"]:
                            page = await call("read_source", {"document_id": doc_id, "page": q["page"], "representation": "original_pdf"})
                            row["original_page"] = {"source_url": page["source_url"], "characters": page["total_characters"],
                                                    "missing_needles": [n for n in q["needles"] if normalize(n) not in normalize(page["text"])]}
                        report["questions"].append(row)
                finally:
                    after = cpu_usage()
                    stop.set()
                    sampler.join(timeout=15)
                    report.setdefault("resources", []).append({"repeat": repeat, "backend": backend,
                        "cpu_seconds": sum(after[k] - before[k] for k in after) / 1e6,
                        "containers": after, "note": "backend全サービスのcgroup CPU差分。計測中のidle処理も含む。MCP/PyMuPDFホスト処理は含まない。"})
                    (run / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    report["finished_at"] = now()
    # Retain portable native evidence separately from the potentially large local archive.
    with gzip.open(run / "native-records.jsonl.gz", "wt") as out:
        for path in sorted(data.glob("*.json")):
            record = json.loads(path.read_text())
            record["original"].pop("pages", None)
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
    (run / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(run / "results.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--questions", default="evaluation/questions.json")
    parser.add_argument("--only-docs", nargs="+")
    parser.add_argument("--output", default="runs/" + datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d-%H%M%S"))
    asyncio.run(evaluate(parser.parse_args()))
