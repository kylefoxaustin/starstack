"""End-to-end and unit tests for starstack. Run: pytest -q
The end-to-end test builds a small synthetic Bayer data set (with darks, hot
pixels, a satellite streak, truncated frames and a corrupt file), stacks it,
and checks the things the README claims."""
import os
import subprocess
import sys

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import starstack as ss  # noqa: E402


# ---------------------------------------------------------------- unit ------
def test_bayer_detector_sees_a_mosaic_and_not_a_plain_image():
    rng = np.random.default_rng(1)
    base = rng.normal(0.2, 0.01, (200, 300)).astype(np.float32)
    mosaic = base.copy()
    mosaic[0::2, 0::2] *= 0.8      # R
    mosaic[1::2, 1::2] *= 0.7      # B  -> greens on the anti-diagonal
    assert ss.detect_bayer(mosaic) == "RGGB"
    assert ss.detect_bayer(base) is None
    flipped = mosaic[::-1]         # what Siril's TOP-DOWN import does
    assert ss.detect_bayer(flipped) == "GBRG"


def test_darks_are_found_by_name():
    assert ss.looks_like_dark("20260201T033720_070_DarkframeMean.tiff")
    assert ss.looks_like_dark("darkframe_003.fit")
    assert ss.looks_like_dark("master_dark_1x0s.fit")
    assert not ss.looks_like_dark("20260201T033719_987_StackInput.tiff")


def test_session_naming(tmp_path):
    sess = tmp_path / "20260201T033715_387"
    sess.mkdir()
    (sess / "manifest.json").write_text('{"nameTarget": "M81 - Bode\'s Galaxy"}')
    assert ss.session_name(str(sess)) == "M81 - Bode's Galaxy"
    bare = tmp_path / "plain"
    bare.mkdir()
    assert ss.session_name(str(bare)) == "plain"


def test_find_sessions_ignores_folders_without_images(tmp_path):
    from astropy.io import fits
    for name, with_img in (("a", True), ("b", True), ("notes", False)):
        d = tmp_path / name
        d.mkdir()
        if with_img:
            fits.PrimaryHDU(np.zeros((8, 8), np.uint16)).writeto(d / "x.fit")
    found = [os.path.basename(p) for p in ss.find_sessions(str(tmp_path))]
    assert found == ["a", "b"]
    assert ss.find_sessions(str(tmp_path / "a")) == []   # a folder of frames is not a night


# ------------------------------------------------------------ end to end ----
@pytest.fixture(scope="module")
def synthetic(tmp_path_factory):
    out = tmp_path_factory.mktemp("frames")
    subprocess.run([sys.executable, os.path.join(ROOT, "make_test_data.py"), str(out), "12"],
                   check=True, capture_output=True)
    return out


def test_stack_end_to_end(synthetic, tmp_path):
    out_tif = tmp_path / "stack.tif"
    report = tmp_path / "frames.csv"
    r = subprocess.run([sys.executable, os.path.join(ROOT, "starstack.py"), str(synthetic),
                        "-o", str(out_tif), "--report", str(report), "-j", "2"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert out_tif.exists()
    log = r.stdout
    assert "12 lights, 8 darks" in log or "13 lights, 8 darks" in log
    assert "debayering as RGGB" in log
    assert "master dark: 8 frames" in log
    assert "done. go outside." in log

    import csv
    rows = list(csv.DictReader(open(report)))
    status = {r["file"]: r["status"] for r in rows}
    assert sum(1 for s in status.values() if s == "unreadable") == 1      # the corrupt file
    assert sum(1 for s in status.values() if s == "dark") == 8
    assert sum(1 for s in status.values() if s == "used") >= 9

    # hot pixels: known positions, should be gone after dark subtraction + clipping
    import tifffile
    stacked = tifffile.imread(str(out_tif)).astype(np.float32)
    hy, hx = np.load(synthetic / "_hot.npy")
    lum = ss.luminance(stacked)
    bg = float(np.median(lum))
    hot_excess = float(np.median(lum[hy, hx]) - bg)
    assert hot_excess < 0.005, f"hot pixels survived: {hot_excess}"
    assert stacked.shape[:2] == (1094, 1452)
