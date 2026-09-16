#!/usr/bin/env python3
"""Run the built exe on a tiny scopepull-shaped archive with FITS lights.

The release build runs this before uploading anything. Reason: 0.2.5-0.2.13
shipped an exe in which `import astropy` failed (the spec excluded
astropy.tests, which astropy imports at start-up), so every FITS frame was
"unreadable" -- and no test ever ran the frozen build on a FITS file. TIFFs
worked, so the M81/M82 runs looked fine.

Self-contained on purpose (needs only requirements.txt): the first version
imported the frame builder from tests/, which imports pytest, which the
release runner doesn't have. That failed the very first gated build.

    python smoke_frozen.py dist/starstack.exe      (or dist/starstack on Linux)
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile

import numpy as np
import tifffile
from astropy.io import fits
from PIL import Image

here = os.path.dirname(os.path.abspath(__file__))
exe = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "dist", "starstack.exe")
if not os.path.exists(exe):
    sys.exit(f"no exe at {exe}")


def odyssey_frame(rng, H=1094, W=1452, shift=(0, 0)):
    """An Odyssey-shaped RGGB mosaic with a few stars (16-bit)."""
    yy, xx = np.mgrid[0:H, 0:W]
    img = np.full((H, W), 2000.0)
    for _ in range(40):
        cy, cx = rng.uniform(40, H - 40) + shift[0], rng.uniform(40, W - 40) + shift[1]
        img += rng.uniform(4000, 20000) * np.exp(-((yy - cy) ** 2 + (xx - cx) ** 2) / 8.0)
    gain = np.ones((H, W))
    gain[0::2, 0::2] = 1.45          # R
    gain[1::2, 1::2] = 1.07          # B
    img = img * gain + rng.normal(0, 60, (H, W))
    return np.clip(img, 0, 65535).astype(np.uint16)


def scopepull_observation(d, rng, n, target):
    """What scopepull's ingest writes: frames/ (TIFF + FITS twins),
    calibration/ (dark, twins too), reference/ (StackSum + preview.jpg),
    observation.json with the scope manifest inside."""
    for sub in ("frames", "calibration", "reference"):
        (d / sub).mkdir(parents=True)
    for i in range(n):
        stem = d / "frames" / f"20260131T0{i:02d}_StackInput"
        fr = odyssey_frame(rng, shift=(rng.integers(-5, 5), rng.integers(-5, 5)))
        tifffile.imwrite(str(stem) + ".tiff", fr)
        h = fits.PrimaryHDU(fr)
        h.header["BAYERPAT"] = "RGGB"; h.header["SENSPAT"] = "GBRG"; h.header["EXPTIME"] = 4.0
        h.writeto(str(stem) + ".fits")
    dark = (rng.normal(2000, 30, (1094, 1452))).clip(0, 65535).astype(np.uint16)
    tifffile.imwrite(str(d / "calibration" / "20260131T000_DarkframeMean.tiff"), dark)
    fits.PrimaryHDU(dark).writeto(str(d / "calibration" / "20260131T000_DarkframeMean.fits"))
    tifffile.imwrite(str(d / "reference" / "20260131T999_StackSum.tiff"), np.zeros((1088, 1452), np.uint16))
    Image.new("RGB", (1452, 1094)).save(d / "reference" / "preview.jpg")
    (d / "observation.json").write_text(json.dumps({
        "scope_manifest": {"nameTarget": target, "expo": 3970000, "gain": 20, "sensor": "IMX415",
                           "obs_attr": {"frames_saved": n, "frames_stacked": n - 1}},
        "catalog": {}, "pulled_at": "2026-02-01T05:00:00+00:00", "bayer_pattern": "RGGB"}))
    (d / "SHA256SUMS").write_text("")


with tempfile.TemporaryDirectory() as td:
    obs = pathlib.Path(td) / "2026-01-31" / "m31__1"
    scopepull_observation(obs, np.random.default_rng(7), 6, "M31 - smoke")
    out = os.path.join(td, "smoke.tif")
    r = subprocess.run([exe, "--cli", str(obs), "-o", out, "--no-log", "-j", "2", "--no-align"],
                       capture_output=True, text=True, errors="replace", timeout=600)
    print(r.stdout[-2500:])
    ok = r.returncode == 0 and os.path.exists(out) and "debayering as RGGB" in r.stdout
    print("FROZEN SMOKE:", "OK" if ok else f"FAILED (exit {r.returncode})")
    sys.exit(0 if ok else 1)
