"""Make a measured corpus usable by the default MCP, preserving other versions."""
import hashlib
import json
import shutil
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
run_data = (root / sys.argv[1] / "data").resolve()
destination = root / "data"
destination.mkdir(exist_ok=True)
latest = {}
for path in run_data.glob("*.json"):
    r = json.loads(path.read_text())
    if r.get("backend") != "crawl4ai":
        continue
    key = r["backend"], r["url"]
    if key not in latest or r["retrieved_at"] > latest[key]["retrieved_at"]:
        latest[key] = r
for r in latest.values():
    original = r["original"]
    if original["success"]:
        source = Path(original["path"])
        if hashlib.sha256(source.read_bytes()).hexdigest() != original["sha256"]:
            raise ValueError("Original hash mismatch: " + str(source))
        target = destination / source.name
        shutil.copyfile(source, target)
        original["path"] = str(target)
    (destination / (r["id"] + ".json")).write_text(json.dumps(r, ensure_ascii=False, indent=2))
print(f"Imported {len(latest)} versions to {destination}")
