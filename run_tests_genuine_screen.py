"""
Genuine + Screen-recapture test harness for the live /verify endpoint.

Manages the server lifecycle:
  1. Kill any stale process on port 8000.
  2. Start uvicorn (venv) as a subprocess — no --reload.
  3. Poll until the server is ready (up to 60 s; model loading takes time).
  4. Run tests.
  5. Terminate the server in a finally block (never orphaned).

Tests two folders:
  ids/original-liveness — genuine cards; ALL four filters must PASS (precision).
  ids/screen-filter     — screen-recapture attacks; takenFromScreen must FAIL (recall).
"""

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

BASE_DIR      = Path(__file__).parent
VENV_UVICORN  = BASE_DIR / "venv" / "bin" / "uvicorn"
VERIFY_URL    = "http://localhost:8000/verify"
HEALTH_URL    = "http://localhost:8000/"
HTTP_TIMEOUT  = 180.0
STARTUP_TIMEOUT = 60.0
IMAGE_EXTS    = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
FILTER_KEYS   = ["takenFromScreen", "takenFromPaper", "hasSuperimposedElements", "alteredByAI"]

TIMESTAMP     = datetime.now().strftime("%Y%m%d_%H%M%S")
RESULTS_PATH  = BASE_DIR / f"genuine_screen_test_results_{TIMESTAMP}.json"


def _resolve_ids_root() -> Path:
    for name in ("ids", "Ids", "IDs", "IDS"):
        cand = BASE_DIR / name
        if cand.is_dir():
            return cand
    return BASE_DIR / "ids"


IDS_ROOT = _resolve_ids_root()


# ── server lifecycle ──────────────────────────────────────────────────────────

def kill_port_8000():
    """Kill any process currently listening on port 8000."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", "tcp:8000"],
            capture_output=True, text=True,
        )
        pids = [p for p in result.stdout.strip().split() if p]
        for pid in pids:
            print(f"[server] killing stale PID {pid} on port 8000")
            subprocess.run(["kill", "-9", pid], capture_output=True)
        if pids:
            time.sleep(1.5)
    except Exception as exc:
        print(f"[server] could not check port 8000: {exc}")


def start_server() -> subprocess.Popen:
    kill_port_8000()
    cmd = [str(VENV_UVICORN), "main:app", "--host", "0.0.0.0", "--port", "8000"]
    print(f"[server] starting: {' '.join(cmd)}")
    return subprocess.Popen(
        cmd,
        cwd=str(BASE_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def wait_for_server(proc: subprocess.Popen, timeout: float = STARTUP_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    print(f"[server] waiting up to {int(timeout)} s for server to be ready ...")
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            print(f"[server] process exited prematurely (rc={proc.returncode})")
            return False
        try:
            r = httpx.get(HEALTH_URL, timeout=3.0)
            if r.status_code < 500:
                print(f"[server] ready (HTTP {r.status_code})")
                return True
        except Exception:
            pass
        time.sleep(2)
    print("[server] timed out waiting for server to start")
    return False


def stop_server(proc: subprocess.Popen):
    if proc is None or proc.poll() is not None:
        return
    print("[server] terminating ...")
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        print("[server] sending SIGKILL")
        proc.kill()
        proc.wait()
    print(f"[server] stopped (rc={proc.returncode})")


# ── image helpers ─────────────────────────────────────────────────────────────

def list_images(folder: Path):
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in IMAGE_EXTS)


def fetch_result(client: httpx.Client, folder_name: str, img_path: Path) -> dict:
    mt = "image/png" if img_path.suffix.lower() == ".png" else "image/jpeg"
    with open(img_path, "rb") as fh:
        resp = client.post(VERIFY_URL, files={"file": (img_path.name, fh, mt)},
                           timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    data      = resp.json()
    filters   = data.get("filters", {})
    scores    = data.get("filter_scores", {})
    breakdown = data.get("filter1_breakdown", {})
    se        = breakdown.get("sightengine_recapture", {})
    cl        = breakdown.get("claude_screen", {})
    return {
        "id":            f"{folder_name}/{img_path.name}",
        "folder":        folder_name,
        "filename":      img_path.name,
        "filters":       {k: filters.get(k) for k in FILTER_KEYS},
        "filter_scores": {k: scores.get(k)  for k in FILTER_KEYS},
        "filter1": {
            "sightengine_result": se.get("result"),
            "sightengine_score":  se.get("score"),
            "claude_result":      cl.get("result"),
            "claude_confidence":  cl.get("confidence"),
        },
        "claude_error": data.get("claude_error", ""),
    }


def print_image_block(img_id: str, raw: dict, tag: str):
    f  = raw["filters"]
    sc = raw["filter_scores"]
    f1 = raw["filter1"]
    print(f"\n--- {img_id}  [{tag}]")
    print(f"    takenFromScreen        : {f['takenFromScreen']}  (score={sc['takenFromScreen']})")
    print(f"        |- Sightengine recap: {f1['sightengine_result']}  (score={f1['sightengine_score']})")
    print(f"        |- Claude screen    : {f1['claude_result']}  (confidence={f1['claude_confidence']})")
    print(f"    takenFromPaper         : {f['takenFromPaper']}  (score={sc['takenFromPaper']})")
    print(f"    hasSuperimposedElements: {f['hasSuperimposedElements']}  (score={sc['hasSuperimposedElements']})")
    print(f"    alteredByAI            : {f['alteredByAI']}  (score={sc['alteredByAI']})")


# ── per-folder record annotation ──────────────────────────────────────────────

def annotate_genuine(raw: dict) -> dict:
    failed  = [k for k in FILTER_KEYS if raw["filters"].get(k) == "fail"]
    outcome = "CORRECT" if not failed else "FALSE_POSITIVE"
    return {**raw, "failed_filters": failed, "outcome": outcome,
            "false_positive_filters": failed}


def annotate_screen(raw: dict) -> dict:
    f  = raw["filters"]
    f1 = raw["filter1"]
    caught = f.get("takenFromScreen") == "fail"

    catch_source = None
    if caught:
        se_fail = f1["sightengine_result"] == "fail"
        cl_fail = f1["claude_result"] == "fail"
        if se_fail and cl_fail:
            catch_source = "both"
        elif se_fail:
            catch_source = "sightengine_only"
        elif cl_fail:
            catch_source = "claude_only"
        else:
            catch_source = "unknown"

    parallel = [k for k in FILTER_KEYS if k != "takenFromScreen" and f.get(k) == "fail"]
    return {**raw, "screen_caught": caught,
            "outcome": "CAUGHT" if caught else "MISS",
            "catch_source": catch_source, "parallel_filters": parallel}


# ── summaries ─────────────────────────────────────────────────────────────────

def build_genuine_summary(records: list) -> dict:
    all_recs  = [r for r in records if r.get("folder") == "original-liveness"]
    recs      = [r for r in all_recs if "error" not in r]
    false_pos = [r for r in recs if r.get("outcome") == "FALSE_POSITIVE"]

    fp_tally: dict = {}
    for r in false_pos:
        for f in r["false_positive_filters"]:
            fp_tally[f] = fp_tally.get(f, 0) + 1

    se_only, cl_only, both = [], [], []
    for r in false_pos:
        if "takenFromScreen" not in r["false_positive_filters"]:
            continue
        se_f = r["filter1"]["sightengine_result"] == "fail"
        cl_f = r["filter1"]["claude_result"] == "fail"
        (both if se_f and cl_f else se_only if se_f else cl_only).append(r["id"])

    return {
        "folder":               "original-liveness",
        "total_images":         len(all_recs),
        "completed":            len(recs),
        "correct_all_pass":     len(recs) - len(false_pos),
        "false_positive_count": len(false_pos),
        "false_positive_files": [
            {"id": r["id"], "fired_filters": r["false_positive_filters"]}
            for r in false_pos
        ],
        "false_positive_filter_tally": fp_tally,
        "screen_false_positive_source": {
            "sightengine_only": se_only,
            "claude_only":      cl_only,
            "both":             both,
        },
    }


def build_screen_summary(records: list) -> dict:
    all_recs = [r for r in records if r.get("folder") == "screen-filter"]
    recs     = [r for r in all_recs if "error" not in r]
    caught   = [r for r in recs if r.get("outcome") == "CAUGHT"]
    missed   = [r for r in recs if r.get("outcome") == "MISS"]

    se_only = [r["id"] for r in caught if r["catch_source"] == "sightengine_only"]
    cl_only = [r["id"] for r in caught if r["catch_source"] == "claude_only"]
    both    = [r["id"] for r in caught if r["catch_source"] == "both"]

    return {
        "folder":        "screen-filter",
        "total_images":  len(all_recs),
        "completed":     len(recs),
        "caught_count":  len(caught),
        "miss_count":    len(missed),
        "missed_files":  [r["id"] for r in missed],
        "catch_source_breakdown": {
            "sightengine_only": se_only,
            "claude_only":      cl_only,
            "both":             both,
        },
        "parallel_failures": [
            {"id": r["id"], "also_fired": r["parallel_filters"]}
            for r in caught if r["parallel_filters"]
        ],
    }


def print_genuine_summary(s: dict):
    print(f"\n[original-liveness]  — GENUINE cards (precision)")
    print(f"  Total completed        : {s['completed']} / {s['total_images']}")
    print(f"  Correct (all-pass)     : {s['correct_all_pass']} / {s['completed']}")
    print(f"  FALSE POSITIVES        : {s['false_positive_count']}")
    for fp in s["false_positive_files"]:
        print(f"      - {fp['id']}  (fired: {', '.join(fp['fired_filters'])})")
    print(f"  Filter tally (FP cause): {s['false_positive_filter_tally'] or '{}'}")
    scr = s["screen_false_positive_source"]
    if any(len(v) for v in scr.values()):
        print(f"  Screen FP source breakdown:")
        print(f"      Sightengine only: {len(scr['sightengine_only'])}")
        for fid in scr["sightengine_only"]:
            print(f"          - {fid}")
        print(f"      Claude only     : {len(scr['claude_only'])}")
        for fid in scr["claude_only"]:
            print(f"          - {fid}")
        print(f"      Both            : {len(scr['both'])}")
        for fid in scr["both"]:
            print(f"          - {fid}")


def print_screen_summary(s: dict):
    print(f"\n[screen-filter]  — SCREEN recapture attacks (recall)")
    print(f"  Total completed   : {s['completed']} / {s['total_images']}")
    print(f"  Caught            : {s['caught_count']} / {s['completed']}")
    print(f"  MISSES            : {s['miss_count']}")
    for fid in s["missed_files"]:
        print(f"      - {fid}")
    src = s["catch_source_breakdown"]
    print(f"  Catch source breakdown:")
    print(f"      Sightengine only: {len(src['sightengine_only'])}")
    for fid in src["sightengine_only"]:
        print(f"          - {fid}")
    print(f"      Claude only     : {len(src['claude_only'])}")
    for fid in src["claude_only"]:
        print(f"          - {fid}")
    print(f"      Both            : {len(src['both'])}")
    for fid in src["both"]:
        print(f"          - {fid}")
    if s["parallel_failures"]:
        print(f"  Parallel (screen caught + other filter also fired):")
        for p in s["parallel_failures"]:
            print(f"      - {p['id']}  (also: {', '.join(p['also_fired'])})")


def print_full_summary(records: list):
    print("\n" + "=" * 70)
    print("PER-FOLDER SUMMARY")
    print("=" * 70)
    print_genuine_summary(build_genuine_summary(records))
    print_screen_summary(build_screen_summary(records))
    print("\n" + "=" * 70)


# ── persistence ───────────────────────────────────────────────────────────────

def make_payload(status: str, records: list) -> dict:
    return {
        "status":    status,
        "timestamp": TIMESTAMP,
        "verify_url": VERIFY_URL,
        "ids_root":  str(IDS_ROOT),
        "records":   records,
        "summary": {
            "original-liveness": build_genuine_summary(records),
            "screen-filter":     build_screen_summary(records),
        },
    }


def save(status: str, records: list):
    RESULTS_PATH.write_text(json.dumps(make_payload(status, records), indent=2))


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    server_proc = None
    records: list = []
    status = "in_progress"

    try:
        server_proc = start_server()
        if not wait_for_server(server_proc):
            print("[fatal] server did not become ready — aborting")
            sys.exit(1)

        folders = [
            ("original-liveness", "genuine"),
            ("screen-filter",     "screen"),
        ]
        work = []
        for folder_name, kind in folders:
            imgs = list_images(IDS_ROOT / folder_name)
            if not imgs:
                print(f"[warn] no images found in {IDS_ROOT / folder_name}")
            for img in imgs:
                work.append((folder_name, kind, img))

        total = len(work)
        print(f"\nIds root : {IDS_ROOT}")
        print(f"Endpoint : {VERIFY_URL}")
        print(f"Total    : {total} images")
        print(f"Results  : {RESULTS_PATH}")

        save("in_progress", records)

        with httpx.Client() as client:
            for idx, (folder_name, kind, img_path) in enumerate(work, 1):
                img_id = f"{folder_name}/{img_path.name}"
                print(f"\n[{idx}/{total}] POST {img_id} ...")
                try:
                    raw = fetch_result(client, folder_name, img_path)

                    if kind == "genuine":
                        rec = annotate_genuine(raw)
                        tag = rec["outcome"]
                        print_image_block(img_id, rec, tag)
                        if rec["outcome"] == "FALSE_POSITIVE":
                            print(f"    !! FALSE POSITIVE — fired: {', '.join(rec['false_positive_filters'])}")
                            f1 = rec["filter1"]
                            if "takenFromScreen" in rec["false_positive_filters"]:
                                print(f"       screen source: SE={f1['sightengine_result']} "
                                      f"Claude={f1['claude_result']}")
                    else:
                        rec = annotate_screen(raw)
                        src = rec["catch_source"] or ""
                        tag = f"{rec['outcome']}" + (f" via {src}" if src else "")
                        print_image_block(img_id, rec, tag)
                        if rec["screen_caught"]:
                            print(f"    >> CAUGHT — source: {rec['catch_source']}")
                            if rec["parallel_filters"]:
                                print(f"    >> parallel: {', '.join(rec['parallel_filters'])}")
                        else:
                            print(f"    !! MISS — takenFromScreen did not fire")

                    records.append(rec)

                except Exception as exc:
                    err = f"{type(exc).__name__}: {exc}"
                    print(f"    !! request/API error: {err}")
                    records.append({
                        "id": img_id, "folder": folder_name, "filename": img_path.name,
                        "error": err,
                        "filters":       {k: None for k in FILTER_KEYS},
                        "filter_scores": {k: None for k in FILTER_KEYS},
                        "filter1": {
                            "sightengine_result": None, "sightengine_score": None,
                            "claude_result": None, "claude_confidence": None,
                        },
                    })

                save("in_progress", records)

        status = "complete"

    except KeyboardInterrupt:
        status = "interrupted"
        print("\n\n[!] KeyboardInterrupt — saving partial results.")
    except Exception as exc:
        status = "error"
        print(f"\n\n[!] Fatal error: {type(exc).__name__}: {exc}")
    finally:
        stop_server(server_proc)

    save(status, records)
    print(f"\nStatus : {status}")
    print(f"Saved  : {RESULTS_PATH}")
    print_full_summary(records)


if __name__ == "__main__":
    main()
