"""
Enrich a CSV of GitHub issue/comment URLs with the underlying title, body, and author.

Input CSV columns:  action, action_time, Repo URL, activity_url
Output CSV adds:    extracted_title, extracted_body, extracted_author, extract_status

Usage:
    python3 extract_github.py <input.csv>

Writes <input>_enriched.csv next to the source. Re-running is safe — rows
already marked extract_status=ok are skipped.
"""
from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

WORKERS = 10
RATE_LIMIT_FLOOR = 50
OUTPUT_COLUMNS = ["extracted_title", "extracted_body", "extracted_author", "extract_status"]

ISSUE_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)")
COMMENT_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/issues/\d+#issuecomment-(\d+)")

_rate_lock = Lock()
_rate_state = {"remaining": 5000, "reset": 0}


def get_token() -> str:
    try:
        out = subprocess.check_output(["gh", "auth", "token"], text=True).strip()
        if not out:
            raise RuntimeError("empty token from `gh auth token`")
        return out
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        sys.exit(f"Could not get token from gh CLI: {e}. Run `gh auth login` first.")


def gh_get(path: str, token: str) -> tuple[int, dict | None]:
    """GET api.github.com{path}. Returns (status, json_body_or_None)."""
    _maybe_sleep_for_rate_limit()
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "github-extractor",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            _record_rate_headers(resp.headers)
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        _record_rate_headers(e.headers)
        return e.code, None
    except (urllib.error.URLError, TimeoutError) as e:
        return 0, {"_network_error": str(e)}


def _record_rate_headers(headers) -> None:
    try:
        remaining = int(headers.get("X-RateLimit-Remaining", _rate_state["remaining"]))
        reset = int(headers.get("X-RateLimit-Reset", _rate_state["reset"]))
        with _rate_lock:
            _rate_state["remaining"] = remaining
            _rate_state["reset"] = reset
    except (TypeError, ValueError):
        pass


def _maybe_sleep_for_rate_limit() -> None:
    with _rate_lock:
        remaining = _rate_state["remaining"]
        reset = _rate_state["reset"]
    if remaining > RATE_LIMIT_FLOOR:
        return
    wait = max(0, reset - int(time.time())) + 2
    if wait > 0:
        print(f"  rate limit low ({remaining}), sleeping {wait}s until reset", flush=True)
        time.sleep(wait)


def parse_url(action: str, url: str) -> tuple[str, dict | None]:
    """Return (kind, params) where kind is 'issue', 'comment', or 'unsupported'."""
    url = url.strip().rstrip("_/ ")
    if action == "COMMENT":
        m = COMMENT_RE.search(url)
        if m:
            owner, repo, comment_id = m.groups()
            return "comment", {"owner": owner, "repo": repo, "comment_id": comment_id}
        m = ISSUE_RE.search(url)
        if m:
            owner, repo, num = m.groups()
            return "issue_fallback", {"owner": owner, "repo": repo, "number": num}
        return "unsupported", None
    if action == "ISSUE_CREATION":
        m = ISSUE_RE.search(url)
        if m:
            owner, repo, num = m.groups()
            return "issue", {"owner": owner, "repo": repo, "number": num}
        if "/pull/" in url:
            return "unsupported_pr", None
        return "unsupported", None
    return "unsupported_action", None


def fetch(action: str, url: str, token: str) -> dict:
    """Returns dict with keys: title, body, author, status."""
    kind, params = parse_url(action, url)
    blank = {"title": "", "body": "", "author": ""}

    if kind == "unsupported":
        return blank | {"status": "unsupported url"}
    if kind == "unsupported_pr":
        return blank | {"status": "pull request not supported"}
    if kind == "unsupported_action":
        return blank | {"status": f"unknown action: {action}"}

    if kind == "comment":
        path = f"/repos/{params['owner']}/{params['repo']}/issues/comments/{params['comment_id']}"
        status, body = gh_get(path, token)
        if status != 200:
            return blank | {"status": _http_msg(status, body)}
        # Comments have no title; pull title from parent issue URL on the comment payload.
        title = ""
        issue_url = body.get("issue_url", "")
        if issue_url:
            s2, parent = gh_get(issue_url.replace("https://api.github.com", ""), token)
            if s2 == 200 and parent:
                title = parent.get("title", "")
        return {
            "title": title,
            "body": body.get("body", "") or "",
            "author": (body.get("user") or {}).get("login", ""),
            "status": "ok",
        }

    if kind in ("issue", "issue_fallback"):
        path = f"/repos/{params['owner']}/{params['repo']}/issues/{params['number']}"
        status, body = gh_get(path, token)
        if status != 200:
            return blank | {"status": _http_msg(status, body)}
        note = "ok" if kind == "issue" else "ok (fell back to parent issue: missing comment id)"
        return {
            "title": body.get("title", "") or "",
            "body": body.get("body", "") or "",
            "author": (body.get("user") or {}).get("login", ""),
            "status": note,
        }

    return blank | {"status": "unhandled"}


def _http_msg(status: int, body: dict | None) -> str:
    if status == 0 and body and "_network_error" in body:
        return f"network error: {body['_network_error']}"
    if status == 404:
        return "404 not found (deleted or private?)"
    if status == 403:
        return "403 forbidden (rate limited or no access)"
    if status == 410:
        return "410 gone"
    return f"http {status}"


def load_done_urls(out_path: str) -> set[str]:
    if not os.path.exists(out_path):
        return set()
    done = set()
    with open(out_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("extract_status", "").startswith("ok"):
                done.add(row["activity_url"])
    return done


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Usage: python3 extract_github.py <input.csv>")
    in_path = sys.argv[1]
    if not os.path.exists(in_path):
        sys.exit(f"Input not found: {in_path}")

    base, ext = os.path.splitext(in_path)
    out_path = f"{base}_enriched{ext}"

    token = get_token()
    done = load_done_urls(out_path)
    if done:
        print(f"Resume mode: {len(done)} rows already enriched, skipping those.")

    with open(in_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        in_cols = reader.fieldnames or []
        rows = list(reader)

    out_cols = in_cols + [c for c in OUTPUT_COLUMNS if c not in in_cols]
    is_new_file = not os.path.exists(out_path)
    out_f = open(out_path, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(out_f, fieldnames=out_cols)
    if is_new_file:
        writer.writeheader()
    write_lock = Lock()

    # Dedupe: same URL fetched once, result fanned out.
    todo = [r for r in rows if r.get("activity_url") and r["activity_url"] not in done]
    unique_urls = {}
    for r in todo:
        unique_urls.setdefault(r["activity_url"], (r["action"], r["activity_url"]))

    print(f"Total rows: {len(rows)}, to fetch: {len(todo)}, unique URLs: {len(unique_urls)}")

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {
            pool.submit(fetch, action, url, token): url
            for url, (action, _) in unique_urls.items()
        }
        for i, fut in enumerate(as_completed(futures), 1):
            url = futures[fut]
            try:
                results[url] = fut.result()
            except Exception as e:
                results[url] = {"title": "", "body": "", "author": "", "status": f"error: {e}"}
            if i % 25 == 0 or i == len(futures):
                print(f"  fetched {i}/{len(futures)}  rate-remaining={_rate_state['remaining']}", flush=True)

    with write_lock:
        for r in todo:
            extracted = results.get(r["activity_url"], {"title": "", "body": "", "author": "", "status": "missed"})
            writer.writerow({
                **{k: r.get(k, "") for k in in_cols},
                "extracted_title": extracted["title"],
                "extracted_body": extracted["body"],
                "extracted_author": extracted["author"],
                "extract_status": extracted["status"],
            })

    out_f.close()

    ok = sum(1 for v in results.values() if v["status"].startswith("ok"))
    print(f"\nDone. {ok}/{len(results)} ok. Output: {out_path}")


if __name__ == "__main__":
    main()
