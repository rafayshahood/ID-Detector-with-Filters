"""
Attack-folder test harness for the live /verify endpoint.

Tests two attack folders that SHOULD be rejected:
  - ids/paper-filter      -> expected filter: takenFromPaper
  - ids/filtro-stickers   -> expected filter: hasSuperimposedElements

An image counts as CORRECT if ANY filter failed (the attack was caught),
regardless of which filter fired.

The server must already be running on http://localhost:8000.

Results JSON is written after EVERY image, so a halted run (Ctrl+C, API
error, out of credit) still leaves a complete-up-to-that-point file on disk.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).parent
VERIFY_URL = "http://localhost:8000/verify"
HTTP_TIMEOUT = 180.0
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

# The Ids folder lives one level up from the project root (kyc-verify/).
# Resolve case-insensitively so "Ids" / "ids" both work.
def _resolve_ids_root() -> Path:
    for name in ("ids", "Ids", "IDs", "IDS"):
        cand = BASE_DIR / name
        if cand.is_dir():
            return cand
    return BASE_DIR / "ids"  # fall back; will error clearly if missing


IDS_ROOT = _resolve_ids_root()

# folder relative name -> expected filter key
FOLDERS = {
    "paper-filter":    "takenFromPaper",
    "filtro-stickers": "hasSuperimposedElements",
}

FILTER_KEYS = ["takenFromScreen", "takenFromPaper", "hasSuperimposedElements", "alteredByAI"]

TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
RESULTS_PATH = BASE_DIR / f"attack_test_results_{TIMESTAMP}.json"


def list_images(folder: Path):
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in IMAGE_EXTS)


def categorize(failed_filters: set, expected: str) -> str:
    """
    CLEAN        - caught by the expected filter ONLY
    PARALLEL     - expected filter fired AND another filter also fired
    WRONG_REASON - caught only by non-expected filter(s); expected did NOT fire
    MISS         - nothing fired (false negative)
    """
    if not failed_filters:
        return "MISS"
    expected_fired = expected in failed_filters
    others = failed_filters - {expected}
    if expected_fired and not others:
        return "CLEAN"
    if expected_fired and others:
        return "PARALLEL"
    return "WRONG_REASON"


def save(payload: dict):
    RESULTS_PATH.write_text(json.dumps(payload, indent=2))


def build_summary(records: list) -> dict:
    """Per-folder summary computed from the records collected so far."""
    summary = {}
    for rel_name, expected in FOLDERS.items():
        recs = [r for r in records if r["folder"] == rel_name and "error" not in r]
        total = len([r for r in records if r["folder"] == rel_name])
        completed = len(recs)

        correct = [r for r in recs if r["failed_filters"]]
        missed  = [r for r in recs if not r["failed_filters"]]

        expected_fired = [r for r in recs if expected in r["failed_filters"]]
        clean    = [r for r in recs if r["category"] == "CLEAN"]
        parallel = [r for r in recs if r["category"] == "PARALLEL"]
        wrong    = [r for r in recs if r["category"] == "WRONG_REASON"]

        # Filter 1 (screen) failure source breakdown
        se_only, claude_only, both = [], [], []
        for r in recs:
            if "takenFromScreen" not in r["failed_filters"]:
                continue
            se_fail = r["filter1"]["sightengine_result"] == "fail"
            cl_fail = r["filter1"]["claude_result"] == "fail"
            if se_fail and cl_fail:
                both.append(r["id"])
            elif se_fail:
                se_only.append(r["id"])
            elif cl_fail:
                claude_only.append(r["id"])

        # Tally of all non-expected filter failures by filter name
        non_expected_tally = {}
        for r in recs:
            for f in r["failed_filters"]:
                if f != expected:
                    non_expected_tally[f] = non_expected_tally.get(f, 0) + 1

        summary[rel_name] = {
            "expected_filter": expected,
            "total_images": total,
            "completed": completed,
            "correct_any_filter": len(correct),
            "missed_count": len(missed),
            "missed_files": [r["id"] for r in missed],
            "expected_filter_fired": len(expected_fired),
            "clean_expected_only": len(clean),
            "parallel_count": len(parallel),
            "parallel_files": [r["id"] for r in parallel],
            "wrong_reason_count": len(wrong),
            "wrong_reason_files": [r["id"] for r in wrong],
            "screen_filter_failures": {
                "sightengine_only": se_only,
                "claude_only": claude_only,
                "both": both,
            },
            "non_expected_failure_tally": non_expected_tally,
        }
    return summary


def print_summary(summary: dict):
    print("\n" + "=" * 70)
    print("PER-FOLDER SUMMARY")
    print("=" * 70)
    for rel_name, s in summary.items():
        print(f"\n[{rel_name}]  expected filter = {s['expected_filter']}")
        print(f"  Total images completed : {s['completed']} / {s['total_images']}")
        print(f"  Correct (caught by ANY filter): {s['correct_any_filter']} / {s['completed']}")
        print(f"  Missed entirely (false negatives): {s['missed_count']}")
        if s["missed_files"]:
            for fid in s["missed_files"]:
                print(f"      - {fid}")
        print(f"  Expected filter fired: {s['expected_filter_fired']}")
        print(f"      caught by expected ONLY (clean): {s['clean_expected_only']}")
        print(f"      with another filter in parallel: {s['parallel_count']}")
        for fid in s["parallel_files"]:
            print(f"          - {fid}")
        print(f"  Caught ONLY by a non-expected filter (wrong reason): {s['wrong_reason_count']}")
        for fid in s["wrong_reason_files"]:
            print(f"      - {fid}")
        scr = s["screen_filter_failures"]
        print(f"  Screen-recapture (Filter 1) failures by source:")
        print(f"      Sightengine only: {len(scr['sightengine_only'])}")
        for fid in scr["sightengine_only"]:
            print(f"          - {fid}")
        print(f"      Claude only     : {len(scr['claude_only'])}")
        for fid in scr["claude_only"]:
            print(f"          - {fid}")
        print(f"      Both            : {len(scr['both'])}")
        for fid in scr["both"]:
            print(f"          - {fid}")
        print(f"  Non-expected filter failures tally: {s['non_expected_failure_tally'] or '{}'}")
    print("\n" + "=" * 70)


def process_image(client: httpx.Client, rel_name: str, expected: str, img_path: Path) -> dict:
    img_id = f"{rel_name}/{img_path.name}"
    mt = "image/png" if img_path.suffix.lower() == ".png" else "image/jpeg"
    with open(img_path, "rb") as fh:
        files = {"file": (img_path.name, fh, mt)}
        resp = client.post(VERIFY_URL, files=files, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    filters = data.get("filters", {})
    scores = data.get("filter_scores", {})
    breakdown = data.get("filter1_breakdown", {})
    se = breakdown.get("sightengine_recapture", {})
    claude_screen = breakdown.get("claude_screen", {})

    failed = {k for k in FILTER_KEYS if filters.get(k) == "fail"}
    category = categorize(failed, expected)

    record = {
        "id": img_id,
        "folder": rel_name,
        "filename": img_path.name,
        "expected_filter": expected,
        "filters": {k: filters.get(k) for k in FILTER_KEYS},
        "filter_scores": {k: scores.get(k) for k in FILTER_KEYS},
        "filter1": {
            "sightengine_result": se.get("result"),
            "sightengine_score":  se.get("score"),
            "claude_result":      claude_screen.get("result"),
            "claude_confidence":  claude_screen.get("confidence"),
        },
        "failed_filters": sorted(failed),
        "category": category,
        "claude_error":      data.get("claude_error", ""),
    }
    return record


def print_record(record: dict):
    f = record["filters"]
    sc = record["filter_scores"]
    f1 = record["filter1"]
    caught = "CAUGHT" if record["failed_filters"] else "MISS"
    print(f"\n--- {record['id']}  [{record['category']}] ({caught})")
    print(f"    takenFromScreen        : {f['takenFromScreen']}  (score={sc['takenFromScreen']})")
    print(f"        |- Sightengine recap: {f1['sightengine_result']}  (score={f1['sightengine_score']})")
    print(f"        |- Claude screen    : {f1['claude_result']}  (confidence={f1['claude_confidence']})")
    print(f"    takenFromPaper         : {f['takenFromPaper']}  (score={sc['takenFromPaper']})")
    print(f"    hasSuperimposedElements: {f['hasSuperimposedElements']}  (score={sc['hasSuperimposedElements']})")
    print(f"    alteredByAI            : {f['alteredByAI']}  (score={sc['alteredByAI']})")
    if record["failed_filters"]:
        print(f"    >> failed: {', '.join(record['failed_filters'])}")


def main():
    records = []
    status = "in_progress"

    def snapshot(st):
        return {
            "status": st,
            "timestamp": TIMESTAMP,
            "verify_url": VERIFY_URL,
            "ids_root": str(IDS_ROOT),
            "folders": FOLDERS,
            "records": records,
            "summary": build_summary(records),
        }

    # Build the full work list up front.
    work = []
    for rel_name, expected in FOLDERS.items():
        folder = IDS_ROOT / rel_name
        imgs = list_images(folder)
        if not imgs:
            print(f"[warn] no images found in {folder}")
        for img in imgs:
            work.append((rel_name, expected, img))

    total = len(work)
    print(f"Ids root : {IDS_ROOT}")
    print(f"Endpoint : {VERIFY_URL}")
    print(f"Total images to test: {total}")
    print(f"Results file: {RESULTS_PATH}")

    # Write an initial (empty) file so the path exists even before image 1.
    save(snapshot(status))

    try:
        with httpx.Client() as client:
            for idx, (rel_name, expected, img) in enumerate(work, 1):
                img_id = f"{rel_name}/{img.name}"
                print(f"\n[{idx}/{total}] POST {img_id} ...")
                try:
                    record = process_image(client, rel_name, expected, img)
                    records.append(record)
                    print_record(record)
                except Exception as exc:
                    err = f"{type(exc).__name__}: {exc}"
                    print(f"    !! request/API error: {err}")
                    records.append({
                        "id": img_id,
                        "folder": rel_name,
                        "filename": img.name,
                        "expected_filter": expected,
                        "error": err,
                        "failed_filters": [],
                        "category": "ERROR",
                        "filters": {k: None for k in FILTER_KEYS},
                        "filter_scores": {k: None for k in FILTER_KEYS},
                        "filter1": {
                            "sightengine_result": None, "sightengine_score": None,
                            "claude_result": None, "claude_confidence": None,
                        },
                    })
                    # Save after the error and continue to the next image.
                    save(snapshot("in_progress"))
                    continue
                # Save after EVERY image.
                save(snapshot("in_progress"))

        status = "complete"
    except KeyboardInterrupt:
        status = "interrupted"
        print("\n\n[!] KeyboardInterrupt — saving partial results and summarizing.")
    except Exception as exc:
        status = "interrupted_error"
        print(f"\n\n[!] Fatal error: {type(exc).__name__}: {exc} — saving partial results.")

    save(snapshot(status))
    print(f"\nStatus: {status}")
    print(f"Saved : {RESULTS_PATH}")
    print_summary(build_summary(records))


if __name__ == "__main__":
    main()
