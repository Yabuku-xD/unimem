#!/usr/bin/env python3
"""Verify the external sources cited by a Markdown research report.

The checker deliberately uses ketch for live source retrieval and stores only
URLs, titles, status, and short excerpts in the output artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


URL_PATTERN = re.compile(r"https://[^)\s]+")


def extract_urls(report: Path) -> list[str]:
    urls = URL_PATTERN.findall(report.read_text(encoding="utf-8"))
    cleaned = [url.rstrip(".,;:]") for url in urls]
    return sorted(set(cleaned))


def scrape_batch(urls: list[str], max_chars: int) -> list[dict[str, Any]]:
    command = ["ketch", "scrape", *urls, "--json", "--max-chars", str(max_chars)]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


def check_sources(report: Path, *, max_chars: int, batch_size: int) -> dict[str, Any]:
    urls = extract_urls(report)
    records: list[dict[str, Any]] = []
    for start in range(0, len(urls), batch_size):
        batch = urls[start : start + batch_size]
        by_url = {item.get("url"): item for item in scrape_batch(batch, max_chars)}
        for url in batch:
            item = by_url.get(url)
            if item is None:
                retry = scrape_batch([url], max_chars)
                item = retry[0] if retry else None
            if item is None:
                records.append({"url": url, "status": "error", "error": "no scrape result"})
                continue
            title = str(item.get("title", "")).strip()
            excerpt = str(item.get("markdown", "")).strip().replace("\n", " ")
            records.append(
                {
                    "url": url,
                    "status": "ok" if title or excerpt else "empty",
                    "title": title,
                    "excerpt": excerpt[:240],
                }
            )
    failed = [record for record in records if record["status"] != "ok"]
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "report": str(report),
        "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
        "source_count": len(records),
        "ok_count": len(records) - len(failed),
        "failed_count": len(failed),
        "sources": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, default=Path("MEMORY_SYSTEM_RESEARCH.md"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/source-check.json"))
    parser.add_argument("--max-chars", type=int, default=240)
    parser.add_argument("--batch-size", type=int, default=8)
    args = parser.parse_args()

    payload = check_sources(args.report, max_chars=args.max_chars, batch_size=args.batch_size)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": payload["failed_count"] == 0,
                "source_count": payload["source_count"],
                "failed_count": payload["failed_count"],
                "output": str(args.output),
            }
        )
    )
    return 0 if payload["failed_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
