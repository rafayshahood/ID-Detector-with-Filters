import asyncio
import base64
import os
import re
import shutil
from pathlib import Path

import httpx
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
_key = ANTHROPIC_API_KEY
print(f"[startup] API key loaded: {bool(_key)}, length: {len(_key)}, prefix: {_key[:8]}")

SE_API_USER   = os.environ["SE_API_USER"]
SE_API_SECRET = os.environ["SE_API_SECRET"]
SE_URL        = "https://api.sightengine.com/1.0/check.json"

BASE_DIR      = Path(__file__).parent
UPLOADS_DIR   = BASE_DIR / "uploads"
SE_INPUTS_DIR = BASE_DIR / "sightengine_inputs"
UPLOADS_DIR.mkdir(exist_ok=True)
SE_INPUTS_DIR.mkdir(exist_ok=True)

app = FastAPI()
app.mount("/uploads",            StaticFiles(directory=str(UPLOADS_DIR)),        name="uploads")
app.mount("/sightengine_inputs", StaticFiles(directory=str(SE_INPUTS_DIR)),      name="sightengine_inputs")
app.mount("/static",             StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return (BASE_DIR / "static" / "index.html").read_text()


async def check_recapture(image_bytes: bytes, filename: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            response = await client.post(
                SE_URL,
                data={"models": "recapture", "api_user": SE_API_USER, "api_secret": SE_API_SECRET},
                files={"media": (filename, image_bytes)},
            )
            data = response.json()
            print(f"[SE recapture] {data}")
            if data.get("status") != "success":
                return {"score": None, "result": "error", "raw": data}
            score = float(data["recapture"]["score"])
            return {"score": score, "result": "fail" if score >= 0.5 else "pass", "raw": data}
    except Exception as e:
        return {"score": None, "result": "error", "raw": str(e)}


async def check_genai(image_bytes: bytes, filename: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            response = await client.post(
                SE_URL,
                data={"models": "genai", "api_user": SE_API_USER, "api_secret": SE_API_SECRET},
                files={"media": (filename, image_bytes)},
            )
            data = response.json()
            print(f"[SE genai] {data}")
            if data.get("status") != "success":
                return {"score": None, "result": "error", "raw": data}
            score = float(data["type"]["ai_generated"])
            return {"score": score, "result": "fail" if score >= 0.5 else "pass", "raw": data}
    except Exception as e:
        return {"score": None, "result": "error", "raw": str(e)}


_CLAUDE_PROMPT = """You are assisting a layered KYC document-authenticity system. You are ONE automated signal feeding into additional checks and human review — NOT the final authority. Be honest about uncertainty; a low confidence score is more useful than forced certainty.

You are given FIVE images:
- IMAGE 1 is a verified GENUINE physical ID card (reference for what a real card looks like).
- IMAGE 2 is an example of a SCREEN RECAPTURE (a photo/screenshot of a card on a screen).
- IMAGE 3 is an example of a PAPER PHOTOCOPY / PRINT of a card. 
- IMAGE 4 is an example of a TAMPERED card (a superimposed/pasted element). 
- IMAGE 5 is the document being verified.

ALL THREE of your verdicts are about IMAGE 5 only. IMAGES 1–4 are reference anchors — compare IMAGE 5 against them. Never give verdicts about IMAGES 1–4.

You will evaluate THREE INDEPENDENT filters on IMAGE 5, each on its OWN axis. A problem belonging to one filter must NOT cause another filter to fail.

=== OVERRIDING RULE FOR ALL THREE FILTERS ===
Falsely rejecting a GENUINE document is the worst outcome and must be avoided. Missing an occasional attack (false negative) is acceptable because other dedicated systems and human review back you up. For every filter: when evidence is clear → FAIL with high confidence; when evidence is mixed, weak, or explainable by lighting/wear/compression/angle → PASS with a LOWER confidence reflecting your uncertainty. When in genuine doubt → PASS. CONFIDENCE (0-100) expresses how sure you are of THAT filter's verdict.

────────────────────────────────────────
FILTER 1 — takenFromScreen (compare IMAGE 5 against the screen-recapture example, IMAGE 2)
Question: Is IMAGE 5 a photo/screenshot of the card displayed on a SCREEN, rather than the physical card itself?
FAIL signals: a mouse cursor, text I-beam, crosshair, or app/UI chrome anywhere; moiré/pixel-grid; uniform backlight glow; visible screen bezel; hard rectangular glass glare.
PASS: real specular highlights, hologram color-shift, card casting its own physical shadow, natural in-hand/on-surface capture.
STAY IN LANE: a paper photocopy is NOT your concern → ignore it for this filter. A pasted sticker/tamper is NOT your concern → ignore it. Only FAIL for screen-display evidence.

────────────────────────────────────────
FILTER 2 — takenFromPaper (compare IMAGE 5 against the photocopy example, IMAGE 3)
Question: Is IMAGE 5 a PRINTED or PHOTOCOPIED reproduction on paper, rather than a real physical card?
FAIL signals: flat matte surface with NO specular highlights; halftone dot/grainy print texture; the "card" conforms to paper/fabric ripples or creases running through it; a printed background scene within the print; paper fiber across the surface; cut-paper edges.
PASS signals: glossy specular highlights, rich saturated colors, sharp guilloche, laser-engraved portrait depth, 3D rigidity with edge shadow, holographic elements catching light. A real card can be OLD, FADED, YELLOWED, WORN — that alone is NOT a copy. Washed-out colors ALONE → PASS.
STAY IN LANE: a screen cursor/recapture is NOT your concern → ignore it. A pasted sticker/tamper is NOT your concern → ignore it. Only FAIL for paper/print evidence.

────────────────────────────────────────
FILTER 3 — hasSuperimposedElements (compare IMAGE 5 against the tamper example, IMAGE 4)
Question: Has something been ADDED, ALTERED, or PLACED on top of IMAGE 5's card after issuance?
Check for: stickers, paper overlays, tape; a pasted/substituted portrait (mismatched edges, different texture, inconsistent lighting on face vs card); altered/covered/painted text fields; covered or altered MRZ; replaced/covered QR or data-matrix; foreign objects on the surface; surface texture inconsistent with surroundings; copy-pasted/cloned/composited regions.
NOT tampering: normal wear, scratches, reflections, glare, shadows, faded colors, camera angle. Only FAIL when highly confident of deliberate tampering.
STAY IN LANE: a screen cursor/recapture is NOT your concern → ignore it. A paper photocopy of an otherwise-intact card is NOT your concern → ignore it. Only FAIL for added/altered elements.

────────────────────────────────────────
=== OUTPUT FORMAT (exactly this, nothing else) ===
FILTER_1_takenFromScreen:
VERDICT: PASS or FAIL
CONFIDENCE: 0-100
REASON: one sentence naming the single most decisive signal

FILTER_2_takenFromPaper:
VERDICT: PASS or FAIL
CONFIDENCE: 0-100
REASON: one sentence naming the single most decisive signal

FILTER_3_hasSuperimposedElements:
VERDICT: PASS or FAIL
CONFIDENCE: 0-100
REASON: one sentence naming the single most decisive signal"""


def _media_type_from_bytes(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/jpeg"


REF_GENUINE = (BASE_DIR / "reference" / "test-02-original.jpg")
REF_SCREEN  = (BASE_DIR / "reference" / "from-screen-02.png")
REF_PAPER   = (BASE_DIR / "reference" / "test-02-paper.jpg")
REF_TAMPER  = (BASE_DIR / "reference" / "photo_5129752219740737265_y.jpg")


def _load_ref(path):
    data = path.read_bytes()
    return base64.standard_b64encode(data).decode("utf-8"), _media_type_from_bytes(data)


REF_GENUINE_B64, REF_GENUINE_MT = _load_ref(REF_GENUINE)
REF_SCREEN_B64,  REF_SCREEN_MT  = _load_ref(REF_SCREEN)
REF_PAPER_B64,   REF_PAPER_MT   = _load_ref(REF_PAPER)
REF_TAMPER_B64,  REF_TAMPER_MT  = _load_ref(REF_TAMPER)


async def check_claude_filters(image_bytes: bytes, media_type: str, filename: str = "") -> tuple[str, str]:
    """Returns (raw_text, error_string). One of them will be empty."""
    try:
        client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY, timeout=1000.0)
        test_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
        img5_label = f"IMAGE 5 — the document being verified (file: {filename}):" if filename else "IMAGE 5 — the document being verified:"
        response = await client.messages.create(
            model="claude-opus-4-8",
            max_tokens=10000,
            thinking={"type": "adaptive"},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "IMAGE 1 — verified genuine physical ID card, sharp hologram and glossy finish (file: test-02-original.jpg):"},
                    {"type": "image", "source": {"type": "base64", "media_type": REF_GENUINE_MT, "data": REF_GENUINE_B64}},
                    {"type": "text", "text": "IMAGE 2 — screen recapture example: a genuine card photographed while displayed on a digital screen (file: from-screen-02.png):"},
                    {"type": "image", "source": {"type": "base64", "media_type": REF_SCREEN_MT, "data": REF_SCREEN_B64}},
                    {"type": "text", "text": "IMAGE 3 — paper photocopy example: a printed or photocopied reproduction of a card on paper (file: test-02-paper.jpg):"},
                    {"type": "image", "source": {"type": "base64", "media_type": REF_PAPER_MT, "data": REF_PAPER_B64}},
                    {"type": "text", "text": "IMAGE 4 — tamper example: a genuine card with a photo sticker superimposed over the portrait field (file: photo_5129752219740737265_y.jpg):"},
                    {"type": "image", "source": {"type": "base64", "media_type": REF_TAMPER_MT, "data": REF_TAMPER_B64}},
                    {"type": "text", "text": img5_label},
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": test_b64}},
                    {"type": "text", "text": _CLAUDE_PROMPT},
                ],
            }],
        )
        raw_text = ""
        for block in response.content:
            if getattr(block, "type", None) == "text":
                raw_text = block.text
                break
        print(f"[Claude] response:\n{raw_text}")
        return raw_text, ""
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        print(f"[Claude] call failed: {err}")
        return "", err


def parse_claude_response(text: str) -> dict:
    _err = {"result": "error", "confidence": None, "reason": ""}
    parsed = {
        "takenFromScreen":         dict(_err),
        "takenFromPaper":          dict(_err),
        "hasSuperimposedElements": dict(_err),
    }
    if not text:
        return parsed

    headers = {
        "takenFromScreen":         r"FILTER_1_takenFromScreen:",
        "takenFromPaper":          r"FILTER_2_takenFromPaper:",
        "hasSuperimposedElements": r"FILTER_3_hasSuperimposedElements:",
    }

    for key, header in headers.items():
        block_match = re.search(
            header + r"(.*?)(?=FILTER_\d|$)", text, re.DOTALL | re.IGNORECASE
        )
        if not block_match:
            continue
        block = block_match.group(1)

        verdict_m = re.search(r"VERDICT:\s*(PASS|FAIL)", block, re.IGNORECASE)
        conf_m    = re.search(r"CONFIDENCE:\s*(\d+)", block)
        reason_m  = re.search(r"REASON:\s*(.+)", block)

        if not verdict_m or not conf_m:
            continue

        parsed[key] = {
            "result":     "pass" if verdict_m.group(1).upper() == "PASS" else "fail",
            "confidence": int(conf_m.group(1)),
            "reason":     reason_m.group(1).strip() if reason_m else "",
        }

    return parsed


def combine_filter1(se_result: str, claude_result: str) -> str:
    if se_result == "fail" or claude_result == "fail":
        return "fail"
    if se_result == "pass" or claude_result == "pass":
        return "pass"
    return "error"


@app.post("/verify")
async def verify(file: UploadFile = File(...)):
    filename      = file.filename
    orig_path     = str(UPLOADS_DIR / f"original_{filename}")
    se_input_path = str(SE_INPUTS_DIR / filename)

    with open(orig_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    shutil.copy2(orig_path, se_input_path)

    orig_bytes  = Path(orig_path).read_bytes()
    detected_mt = _media_type_from_bytes(orig_bytes)
    print(f"[verify] filename={filename} media_type={detected_mt} bytes={len(orig_bytes)}")

    results = await asyncio.gather(
        check_recapture(orig_bytes, filename),
        check_genai(orig_bytes, filename),
        check_claude_filters(orig_bytes, detected_mt, filename),
        return_exceptions=True,
    )

    recapture_res = results[0] if not isinstance(results[0], Exception) \
        else {"score": None, "result": "error", "raw": str(results[0])}
    genai_res     = results[1] if not isinstance(results[1], Exception) \
        else {"score": None, "result": "error", "raw": str(results[1])}
    claude_tuple  = results[2] if not isinstance(results[2], Exception) \
        else ("", str(results[2]))
    claude_raw, claude_error = claude_tuple

    cf               = parse_claude_response(claude_raw)
    filter1_combined = combine_filter1(recapture_res["result"], cf["takenFromScreen"]["result"])

    return JSONResponse({
        "original_image_url":    f"/uploads/original_{filename}",
        "sightengine_input_url": f"/sightengine_inputs/{filename}",
        "claude_input_url":      f"/uploads/original_{filename}",
        "filters": {
            "takenFromScreen":         filter1_combined,
            "takenFromPaper":          cf["takenFromPaper"]["result"],
            "hasSuperimposedElements": cf["hasSuperimposedElements"]["result"],
            "alteredByAI":             genai_res["result"],
            "takenInRealLife":         "pending",
        },
        "filter_scores": {
            "takenFromScreen":         None,
            "takenFromPaper":          cf["takenFromPaper"]["confidence"],
            "hasSuperimposedElements": cf["hasSuperimposedElements"]["confidence"],
            "alteredByAI":             genai_res["score"],
        },
        "filter_reasons": {
            "takenFromScreen":         cf["takenFromScreen"]["reason"],
            "takenFromPaper":          cf["takenFromPaper"]["reason"],
            "hasSuperimposedElements": cf["hasSuperimposedElements"]["reason"],
        },
        "filter1_breakdown": {
            "sightengine_recapture": {
                "result": recapture_res["result"],
                "score":  recapture_res["score"],
            },
            "claude_screen": {
                "result":     cf["takenFromScreen"]["result"],
                "confidence": cf["takenFromScreen"]["confidence"],
                "reason":     cf["takenFromScreen"]["reason"],
            },
        },
        "claude_raw":      claude_raw,
        "claude_error":    claude_error,
        "sightengine_raw": {
            "recapture": recapture_res["raw"],
            "genai":     genai_res["raw"],
        },
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", reload=True, host="0.0.0.0", port=8000)
