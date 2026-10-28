# PICC web measurement tool

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.23690009.svg)](https://doi.org/10.5281/zenodo.23690009)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A web application for collecting **expert endpoint measurements, with per-item
timing**, on chest radiographs. It was built to provide the reader arm of an
AI-versus-radiologist comparison for PICC (peripherally inserted central
catheter) insertion-length prediction, and it is released so that the reader
protocol behind that comparison can be inspected rather than taken on trust.

Korean documentation: [`README.ko.md`](README.ko.md).

---

## What it measures

Eight linear indicators feed the insertion-length regression:

| Key | Indicator |
|---|---|
| `right_clavicle` / `left_clavicle` | clavicle length (contralateral to the access side) |
| `right_hvd` / `left_hvd` | horizontal vertebral distance (ipsilateral) |
| `right_tcd` / `left_tcd` | thoracic cage distance (ipsilateral) |
| `second_ribs_width` | second ribs width (2RHD) |
| `two_vbu` | two vertebral body units (2VBU) |

Every uploaded radiograph is a target for all eight. One radiograph therefore
yields eight tasks per reader.

## Measurement protocol

One task is **one (radiograph × indicator) pair**, not one radiograph with
eight measurements on it:

```
briefing (no image shown)
   → reader presses "start"  →  image revealed          [t0]
   → one drag: press, move, release                     [t1]
   → confirmation popup, placed beside the measurement
        ├ confirm           → next task
        ├ request re-measure→ discarded, pair returns to the pool
        └ confirm and stop  → session can be resumed later
```

`duration_ms = t1 − t0`. Reading the briefing and deliberating over the
confirmation are **outside** the interval; what remains is the time to look at
the image and place two endpoints, which is the interval comparable to model
inference time.

Design decisions that the numbers depend on:

- **Two endpoints for every indicator**, including the clavicles and 2VBU.
  A length is determined by its two ends, and a uniform gesture keeps the
  timing comparable across indicators.
- **Task order is fully random** over all unmeasured (image × indicator)
  pairs. Presenting one radiograph's eight indicators consecutively would
  induce anchoring, which artificially narrows interobserver spread and
  shortens measurement times.
- **A re-measure request is not re-measured on the spot.** The pair returns to
  the pool for later random re-issue; measuring again while the previous
  attempt is still in mind is biased. Discarded attempts are retained with
  `status='redo'` and excluded from aggregation.
- **Images are prefetched.** If a network fetch happened at the moment of
  reveal, that latency would land inside `duration_ms`.
- **Readers are blinded by construction.** The interface does not expose a
  reader's own earlier measurements, the other readers' measurements, or the
  model output while measuring. The comparison view is gated behind an
  administrator switch *and* completion of that reader's own queue.
- **A separate practice pool.** Practice records are excluded from every
  export and from all administrator statistics at a single choke point
  (`app/export.py::_active_images()`).

## Physical units

Pixel size is resolved in this order:

1. `PixelSpacing` (0028,0030)
2. `ImagerPixelSpacing` (0018,1164)
3. manual entry by an administrator

This matters: in the test set this tool was built for, roughly 23% of studies
carry no `PixelSpacing` but all carry `ImagerPixelSpacing`. Reading only the
first tag and falling back to a constant mis-scales about a quarter of the
cohort, and the resulting error is easily mistaken for model error. The same
resolved value is applied to reader measurements and to model predictions.

Coordinates are stored in **original DICOM pixels**, so exported annotations
share the coordinate system of the training annotations.

## Interobserver agreement

For each (image, indicator), `spread_mm = max − min` over valid measurements is
compared with an administrator-set global tolerance. Exceeding it flags the
item.

**The flag is descriptive only.** It does not alter, drop or replace any
measurement, there is no consensus or arbitration step, and no reference
"ground truth" is produced. Changing the tolerance recomputes the display and
nothing else. Exported annotations are written **per reader**, not merged.

## Exports

XLSX workbook, long-form CSV (`measurements_long.csv`), per-reader annotation
JSON bundles matching the training-annotation format, and a DICOM bundle.

---

## Install

Python 3.12.

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env            # set INITIAL_ADMIN_PASSWORD before first run
python -m uvicorn app.main:app --host 127.0.0.1 --port 1531
```

The application refuses to create the first administrator account with an
empty or trivial password. A password change is forced at first login.

`pylibjpeg-openjpeg` is required for JPEG 2000 transfer syntax, which covers
most of the cohort this tool was used on.

## Layout

```
app/            FastAPI application
  models.py       User · Image · Measurement · Prediction · Setting
  variables.py    the eight indicators: coefficients, briefing text, guides
  geometry.py     pixel → mm
  agreement.py    spread_mm and tolerance comparison
  export.py       XLSX / CSV / annotation JSON / DICOM bundles
  routers/        public · reviewer (measuring) · admin
templates/      Jinja2
static/         vanilla JS canvas; no frontend framework
scripts/        dev-reload supervisor, optional inference bridge
tests/          end-to-end suites, each against an isolated temporary server
docs/           operations guide (source + builder)
```

No frontend build step and no compiled dependencies: password hashing is
stdlib PBKDF2-SHA256. The tool is meant to be deployable on an isolated
network.

## Tests

```bash
.venv/bin/python tests/e2e_review_gate.py     # permissions, menu exposure (32)
.venv/bin/python tests/e2e_matrix_detail.py   # comparison view (26)
.venv/bin/python tests/e2e_pool.py            # practice/real separation
.venv/bin/python tests/e2e_smoke.py           # full suite (needs sample DICOM)
```

Each suite starts its **own** server on its own port with a temporary
database, and refuses to touch a live instance unless `--live` is passed. This
is not incidental: an early smoke run was pointed at the live server, and its
final step — verifying the reset safeguard — called `/admin/reset` for real and
destroyed a reader's work in progress.

## Not included

- **Radiographs.** The images contain protected health information and are not
  distributable.
- **Trained model weights.** `scripts/model.py` carries the landmark network
  used in the study — an EfficientNet-B0 backbone whose stride-8, -16 and -32
  features are fused into a 64 x 64 heatmap per landmark point, decoded to
  continuous coordinates by spatial softmax and soft-argmax. The trained
  weights are not distributable here. `scripts/picc_infer_core.py` loads them
  from a directory given at run time, and `scripts/predict_stream.py` is the
  bridge the application calls; without weights the AI-measurement feature is
  inactive and everything else works.

## Licence and citation

MIT, see [`LICENSE`](LICENSE). Bundled fonts are under the SIL Open Font
License — see [`static/fonts/NOTICE.md`](static/fonts/NOTICE.md).

Citation metadata: [`CITATION.cff`](CITATION.cff).
