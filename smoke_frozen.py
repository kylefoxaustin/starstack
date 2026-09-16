#!/usr/bin/env python3
"""Run the built exe on a tiny scopepull-shaped archive with FITS lights.

The release build ran this from 0.2.14 on, before uploading anything.
Reason: 0.2.5-0.2.13 shipped an exe in which `import astropy` failed (the
spec excluded astropy.tests, which astropy imports at start-up), so every
FITS frame was "unreadable" -- and no test ever ran the frozen build on a
FITS file. TIFFs worked, so the M81/M82 runs looked fine.

    python smoke_frozen.py dist/starstack.exe      (or dist/starstack on Linux)
"""
import os
import pathlib
import subprocess
import sys
import tempfile

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
sys.path.insert(0, os.path.join(here, "tests"))

exe = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "dist", "starstack.exe")
if not os.path.exists(exe):
    sys.exit(f"no exe at {exe}")

import numpy as np                                            # noqa: E402
from test_starstack import _scopepull_observation             # noqa: E402

with tempfile.TemporaryDirectory() as td:
    obs = pathlib.Path(td) / "2026-01-31" / "m31__1"
    _scopepull_observation(obs, np.random.default_rng(7), 6, "M31 - smoke")
    out = os.path.join(td, "smoke.tif")
    r = subprocess.run([exe, "--cli", str(obs), "-o", out, "--no-log", "-j", "2", "--no-align"],
                       capture_output=True, text=True, errors="replace", timeout=600)
    print(r.stdout[-2500:])
    ok = r.returncode == 0 and os.path.exists(out) and "debayering as RGGB" in r.stdout
    print("FROZEN SMOKE:", "OK" if ok else f"FAILED (exit {r.returncode})")
    sys.exit(0 if ok else 1)
