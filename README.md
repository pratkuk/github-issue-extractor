# GitHub Issue & Comment Extractor

Takes a CSV of GitHub issue or comment URLs and writes a new CSV with the title, body, and author of each one filled in.

Useful when you have a list of GitHub activity (e.g. from a community-monitoring tool) and need the actual content of each issue/comment for downstream analysis or AI classification.

---

## What it does

**Input CSV** must have these 4 columns:

| Column | Example |
|---|---|
| `action` | `ISSUE_CREATION` or `COMMENT` |
| `action_time` | `05/04/26 12:42` |
| `Repo URL` | `https://github.com/google/gvisor` |
| `activity_url` | `https://github.com/google/gvisor/issues/12198#issuecomment-4188833555` |

**Output CSV** is identical plus 4 new columns appended on the right:

| Column | What goes there |
|---|---|
| `extracted_title` | Issue title. For comments, the parent issue's title. |
| `extracted_body` | Full markdown body of the issue or comment, verbatim. |
| `extracted_author` | GitHub login of the author. |
| `extract_status` | `ok`, or a short reason if it failed (e.g. `404 not found`, `pull request not supported`). |

---

## Setup (one-time)

### 1. Make sure `gh` is installed and you're logged in

```bash
gh auth status
```

If it says you're not logged in, run:

```bash
gh auth login
```

The script reads your token from `gh` automatically — there's no `.env` file or service account to manage. Your `gh` login needs at least `repo` scope to access private repos you have rights to.

### 2. That's it

The script uses only the Python standard library — no `pip install` needed. Python 3.9+ recommended.

---

## Running it

```bash
python3 extract_github.py path/to/input.csv
```

Output is written to `path/to/input_enriched.csv` (alongside the source). The original CSV is never modified.

### Example

```bash
python3 extract_github.py ~/Downloads/issues_to_extract.csv
```

```
Total rows: 850, to fetch: 850, unique URLs: 847
  fetched 25/847  rate-remaining=4975
  fetched 50/847  rate-remaining=4950
  ...
  fetched 847/847  rate-remaining=4153
Done. 843/847 ok. Output: /Users/you/Downloads/issues_to_extract_enriched.csv
```

---

## Behaviour & guarantees

- **Resume on rerun.** If the run is interrupted (Ctrl-C, network drop, laptop sleep), just run the same command again. Rows already marked `ok` in the output file are skipped.
- **Dedupe.** If the same URL appears multiple times in the input, it's fetched once and the result is fanned out to every row.
- **Parallelism.** 10 concurrent requests by default. To change it, edit `WORKERS = 10` near the top of `extract_github.py`. Don't go above ~25 — GitHub's secondary rate limits will start rejecting bursts.
- **Rate limits.** Authenticated `gh` users get 5,000 requests/hour. The script watches the `X-RateLimit-Remaining` header and pauses automatically if you get within 50 of the limit.
- **Comment rows make 2 calls.** One for the comment body, one for the parent issue's title. If you have a CSV that's mostly comments, expect the rate-limit usage to roughly double.
- **Failures are silent per-row.** If a URL is dead, private, or malformed, that row gets a blank body and a one-line reason in `extract_status`. The run keeps going.

---

## What it can and can't handle

| URL shape | Behaviour |
|---|---|
| `.../issues/123` | ✅ Fetched as an issue |
| `.../issues/123#issuecomment-456` | ✅ Fetched as a comment, with parent issue title |
| `.../issues/123#issuecomment-456__` (trailing junk) | ✅ Trailing characters are stripped |
| `.../issues/123#issuecomment-` (missing comment ID) | ⚠️ Falls back to fetching the parent issue. Status reads `ok (fell back to parent issue: missing comment id)` |
| `.../pull/123` | ❌ Marked `pull request not supported`. PRs are intentionally out of scope. |
| Anything else | ❌ Marked `unsupported url` |

---

## Common `extract_status` values

| Status | Meaning |
|---|---|
| `ok` | Successfully fetched |
| `ok (fell back to parent issue: missing comment id)` | Comment URL was malformed, returned the parent issue instead |
| `404 not found (deleted or private?)` | Issue was deleted, or you don't have access |
| `403 forbidden (rate limited or no access)` | Hit a rate limit, or repo permissions block you |
| `410 gone` | Resource permanently removed |
| `pull request not supported` | URL points to a PR, not an issue |
| `unsupported url` | URL didn't match any known GitHub issue/comment pattern |
| `network error: ...` | Transient network problem (rerun to retry just this row) |

---

## FAQ

**Q: The output CSV has weird quoting around bodies with newlines.**
That's standard CSV escaping. Open it in Excel, Google Sheets, or a CSV-aware tool — they all handle it correctly. Don't open it in a plain text editor and panic.

**Q: How long does this take for N rows?**
Roughly `N / 1500` minutes at the default 10 workers. So 3,000 rows ≈ 2 minutes, 10,000 rows ≈ 7 minutes. The rate limit will not be a problem unless you're processing > 5,000 rows in an hour.

**Q: Can I run it on private repos?**
Yes, as long as your `gh` login has access to them. The script uses your personal token.

**Q: Does it modify the original CSV?**
No. It only writes to `<input>_enriched.csv`. If you re-run, it appends to that output file and skips already-done rows.

**Q: I added new rows to my input CSV — can I resume?**
Yes. Run the same command. Existing `ok` rows are skipped, only new rows are fetched.

---

## Files in this folder

```
github-extractor/
├── extract_github.py    ← the script
└── README.md            ← this file
```

No config, no dependencies, no service accounts. Just the script.
