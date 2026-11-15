"""Summarize repeat measurements without turning substring checks into QA scores."""
import json
import re
import statistics
import sys
from pathlib import Path


def mib(s):
    amount, unit = re.fullmatch(r"([\d.]+)([A-Za-z]+)", s.split(" / ")[0]).groups()
    return float(amount) * {"B": 1 / 1024**2, "KiB": 1 / 1024, "MiB": 1, "GiB": 1024}[unit]


def summarize(run):
    report = json.loads((run / "results.json").read_text())
    summary = {}
    corpus = {d["id"]: d for d in json.loads((run / "corpus.json").read_text())}
    for backend in report["backends"]:
        fetches = [f for f in report["fetches"] if f["backend"] == backend]
        questions = [q for q in report["questions"] if q["backend"] == backend]
        links = [d for d in report["discovery"] if d["backend"] == backend]
        peaks = []
        for path in run.glob(f"{backend}-*-stats.jsonl"):
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            count = 1 if backend == "crawl4ai" else 5
            for i in range(0, len(rows), count):
                batch = rows[i:i + count]
                if len({r["Name"] for r in batch}) != count:
                    raise ValueError(f"Incomplete Docker sample: {path}")
                peaks.append(sum(mib(r["MemUsage"]) for r in batch))
        metrics = {}
        for group, selected in [("all", fetches), ("core", [f for f in fetches if f["group"] == "core"]),
                                ("core_html", [f for f in fetches if f["group"] == "core" and corpus[f["doc"]]["kind"] == "html"]),
                                ("core_pdf", [f for f in fetches if f["group"] == "core" and corpus[f["doc"]]["kind"] == "pdf"])]:
            if not selected:
                metrics[group] = {"attempts": 0}
                continue
            metrics[group] = {"attempts": len(selected), "api_success": sum(f["native_success"] for f in selected),
                              "median_native_seconds": statistics.median(f["native_seconds"] for f in selected),
                              "median_total_seconds": statistics.median(f["total_seconds"] for f in selected),
                              "min_native_seconds": min(f["native_seconds"] for f in selected),
                              "max_native_seconds": max(f["native_seconds"] for f in selected)}
        cpu = [r["cpu_seconds"] for r in report.get("resources", []) if r["backend"] == backend]
        summary[backend] = {"fetch": metrics, "questions": {"checks": len(questions),
            "top5_reached": sum(q["rank"] is not None for q in questions),
            "needles_present": sum(q["native_coverage"] for q in questions),
            "warning": "語句包含・検索到達率。回答正解率ではない。表の対応等はmanual-review.json参照。"},
            "discovery": {"targets": sum(len(d["targets"]) for d in links),
                          "found": sum(sum(d["targets"].values()) for d in links)},
            "max_sampled_stack_memory_mib": max(peaks), "median_workload_cpu_seconds": statistics.median(cpu)}
    (run / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    summarize(Path(sys.argv[1]))
