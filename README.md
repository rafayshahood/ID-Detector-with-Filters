# KYC Verifier

A layered KYC document-authenticity API that runs four independent fraud filters on uploaded ID card images:

- **takenFromScreen** — detects whether the card was photographed from a screen (Sightengine recapture model + Claude)
- **takenFromPaper** — detects printed or photocopied reproductions (Claude)
- **hasSuperimposedElements** — detects stickers, pasted portraits, or any element added after issuance (Claude)
- **alteredByAI** — detects AI-generated images (Sightengine genai model)

## Prerequisites

- Python 3.10 or higher
- An Anthropic API key
- Sightengine API credentials (api_user + api_secret)

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/rafayshahood/ID-Detector-with-Filters.git
cd kyc-verify
```

### 2. Create and activate a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure credentials

```bash
cp .env.example .env
```

Open `.env` and fill in your real values:

```
ANTHROPIC_API_KEY=sk-ant-...
SE_API_USER=your_sightengine_api_user
SE_API_SECRET=your_sightengine_api_secret
```

## Test data setup

The test images are not included in this repository. Download the `ids/` folder from Google Drive:

**[Download ids/ from Google Drive](https://drive.google.com/drive/folders/1CaWxjoVHHFpGISNYEC9sN8SwD1Akk08V?usp=sharing)**

Place the downloaded `ids/` folder **inside** the `kyc-verify/` directory (same level as `main.py`), so the structure looks like:

```
kyc-verify/
├── ids/
│   ├── original-liveness/
│   ├── screen-filter/
│   ├── paper-filter/
│   └── filtro-stickers/
├── main.py
└── ...
```

## Running the app

```bash
source venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8000
```

Then open **http://localhost:8000** in your browser to use the web UI, or POST an image directly to `http://localhost:8000/verify`.

## Running the test scripts

Make sure `ids/` is in place before running either script.

### Attack filter test — paper copies and sticker tampering

```bash
source venv/bin/activate
python run_tests_attack.py
```

Tests `ids/paper-filter/` (expected: `takenFromPaper` fires) and `ids/filtro-stickers/` (expected: `hasSuperimposedElements` fires). The server must already be running before you call this script.

### Genuine + screen recapture test

```bash
source venv/bin/activate
python run_tests_genuine_screen.py
```

Tests `ids/original-liveness/` (precision — all four filters must pass on genuine cards) and `ids/screen-filter/` (recall — `takenFromScreen` must fire on recaptures). This script manages the server lifecycle itself: it kills any process on port 8000, starts the server, waits for it to be ready, runs all tests, then shuts the server down.

### Inspecting results

Both scripts write a timestamped JSON file (e.g. `attack_test_results_20260605_024026.json`) after every image, so a partial run is never lost. The file contains a `summary` block with per-folder counts (correct, false positives/negatives, catch-source breakdown) and a `records` array with the full per-image verdict, scores, and filter breakdown.
