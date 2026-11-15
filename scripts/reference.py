"""Fetch original sources for human ground-truth review, independently of contenders."""
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx
import pymupdf
from bs4 import BeautifulSoup

BASE = "https://www.city.abiko.chiba.jp"
SOURCES = {
    "garbage": "/kurashi/gomi_shigen/wakekata/dasikata/wakekatadasikata.html",
    "child": "/kosodate/children/josei_kyufu/specialbenefits/h26jidou.html",
    "move": "/kurashi/jumin/jyumintouroku/tennyu.html",
    "disaster": "/anshin/bousai/hinan/hinanmeirei.html",
    "council": "/shisei/shigikai/teirei_rinji/r6dai2kai6gatsu/index.html",
    "schedule": "/shisei/shigikai/teirei_rinji/r6dai2kai6gatsu/r6-2kaikinittei.html",
    "bills": "/shisei/shigikai/teirei_rinji/r6dai2kai6gatsu/r6-6-gian.html",
    "questions": "/shisei/shigikai/teirei_rinji/r6dai2kai6gatsu/r6-6ippanshitsumon.html",
    "votes": "/shisei/shigikai/teirei_rinji/r6dai2kai6gatsu/index.files/saiketukekkaR6.6.pdf",
    "policy": "/shisei/shiseihoushin/r6_ippan/2024shiseihoushin.html",
    "budget": "/shisei/zaisei/tousho_gaiyou/R6tousyo_gaiyou.html",
    "plan": "/shisei/keikauhoushin/sougoukeikaku/r4_15dai4ji/index.html",
    "minutes_gateway": "/shisei/shigikai/system.html",
}


def snapshot(name, url):
    out = Path("evidence/reference")
    out.mkdir(parents=True, exist_ok=True)
    r = httpx.get(url, timeout=60, follow_redirects=True)
    r.raise_for_status()
    pdf = r.content.startswith(b"%PDF")
    (out / f"{name}.{'pdf' if pdf else 'html'}").write_bytes(r.content)
    if pdf:
        doc = pymupdf.open(stream=r.content, filetype="pdf")
        pages = [{"page": i + 1, "text": p.get_text(sort=True)} for i, p in enumerate(doc)]
        title, links = doc.metadata.get("title", ""), []
        text = "\n\n".join(f"--- PDF page {p['page']} ---\n{p['text']}" for p in pages)
    else:
        s = BeautifulSoup(r.content, "html.parser")
        title = s.title.get_text(strip=True) if s.title else ""
        main = s.select_one("#main") or s
        links = [{"text": a.get_text(" ", strip=True), "url": urljoin(str(r.url), a['href'])}
                 for a in main.select("a[href]") if not a['href'].startswith('#')]
        for tag in main.select("script, style"):
            tag.decompose()
        text = main.get_text("\n", strip=True)
        pages = []
    record = {"id": name, "url": url, "final_url": str(r.url), "title": title,
              "retrieved_at": datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(),
              "sha256": hashlib.sha256(r.content).hexdigest(), "status": r.status_code,
              "kind": "pdf" if pdf else "html", "links": links, "pages": pages}
    (out / f"{name}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2))
    (out / f"{name}.txt").write_text(text)
    print(name, len(text), flush=True)


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3:
        snapshot(sys.argv[1], sys.argv[2])
    else:
        for name, path in SOURCES.items():
            snapshot(name, BASE + path)
            time.sleep(1)
