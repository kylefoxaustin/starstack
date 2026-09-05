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


def test_classify_names():
    assert ss.classify_name("20260201T033719_987_StackInput.tiff") == "light"
    assert ss.classify_name("20260201T033720_070_DarkframeMean.tiff") == "dark"
    assert ss.classify_name("20260201T042554_221_StackSum.tiff") == "extra"
    assert ss.classify_name("preview.jpg") == "extra"
    assert ss.classify_name("flat_0001.fit") == "flat"
    assert ss.classify_name("bias_0001.fit") == "bias"
    assert ss.classify_name("dark_flat_0001.fit") == "darkflat"


def _fake_frames(d, n, prefix="sub", dark=0):
    from astropy.io import fits
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        fits.PrimaryHDU(np.full((8, 8), 100, np.uint16)).writeto(d / f"{prefix}_{i:03d}.fit")
    for i in range(dark):
        fits.PrimaryHDU(np.full((8, 8), 5, np.uint16)).writeto(d / f"darkframe_{i:03d}.fit")


def test_sorted_layout_is_one_session_and_darks_are_not_a_target(tmp_path):
    m13 = tmp_path / "M13"
    _fake_frames(m13 / "lights", 3)
    _fake_frames(m13 / "darks", 0, dark=2)
    assert ss.layout_dirs(str(m13)).keys() == {"light", "dark"}
    assert ss.find_sessions(str(m13)) == []                       # not a night of two targets
    m27 = tmp_path / "M27"
    _fake_frames(m27 / "lights", 3)
    found = [os.path.basename(p) for p in ss.find_sessions(str(tmp_path))]
    assert found == ["M13", "M27"]


def test_sort_folder_moves_the_pile_into_subfolders(tmp_path):
    d = tmp_path / "dump"
    _fake_frames(d, 4, prefix="StackInput", dark=1)
    from astropy.io import fits
    fits.PrimaryHDU(np.zeros((6, 8), np.uint16)).writeto(d / "StackSum.fit")
    (d / "manifest.json").write_text("{}")
    lines = []
    assert ss.sort_folder(str(d), dry_run=True, log=lines.append) == 0
    assert not (d / "lights").exists()                            # dry run moved nothing
    assert ss.sort_folder(str(d), dry_run=False, log=lines.append) == 0
    assert sorted(os.listdir(d / "lights")) == [f"StackInput_{i:03d}.fit" for i in range(4)]
    assert os.listdir(d / "darks") == ["darkframe_000.fit"]
    assert os.listdir(d / "extras") == ["StackSum.fit"]
    assert (d / "manifest.json").exists()                         # stayed put
    assert ss.layout_dirs(str(d)).keys() == {"light", "dark"}
    assert ss.sort_folder(str(d), dry_run=False, log=lines.append) == 0   # idempotent


def _seestar_frame(rng, H=1080, W=1920, shift=(0, 0)):
    """A Seestar-shaped GRBG mosaic: portrait 1080x1920, redder sky than blue,
    a few stars. Returns uint16 like the S50 writes."""
    yy, xx = np.mgrid[0:H, 0:W]
    scene = np.full((H, W), 0.08, np.float32)
    srng = np.random.default_rng(11)             # same star field every frame
    stars = list(zip(srng.uniform(40, W - 40, 60), srng.uniform(40, H - 40, 60), srng.uniform(0.2, 0.8, 60)))
    for sx, sy, amp in stars:
        x0, y0 = int(sx + shift[0]), int(sy + shift[1])
        ys, xs = slice(max(0, y0 - 8), y0 + 9), slice(max(0, x0 - 8), x0 + 9)
        sub_yy, sub_xx = yy[ys, xs], xx[ys, xs]
        scene[ys, xs] += amp * np.exp(-(((sub_xx - sx - shift[0]) ** 2 + (sub_yy - sy - shift[1]) ** 2) / (2 * 2.2 ** 2))).astype(np.float32)
    gain = np.ones((H, W), np.float32)          # GRBG: (0,0)=G (0,1)=R (1,0)=B (1,1)=G
    gain[0::2, 1::2] = 0.9                      # R
    gain[1::2, 0::2] = 0.6                      # B  -> sky is redder than blue
    frame = scene * gain + rng.normal(0, 0.004, (H, W)).astype(np.float32)
    return np.clip(frame * 65535, 0, 65535).astype(np.uint16)


def test_seestar_myworks_layout(tmp_path):
    """MyWorks/M81/ holds the finished stack; MyWorks/M81_sub/ holds the
    Light_*.fit sub-frames with GRBG in the header, plus a .jpg and _thn.jpg
    per sub. The night is one session named M81, debayered as GRBG."""
    from astropy.io import fits
    from PIL import Image
    rng = np.random.default_rng(3)
    works = tmp_path / "MyWorks"
    res, sub = works / "M81", works / "M81_sub"
    res.mkdir(parents=True); sub.mkdir()
    # the finished stack the scope made (RGB planes) + its jpg
    fits.PrimaryHDU(np.zeros((3, 1080, 1920), np.uint16)).writeto(res / "M81.fit")
    Image.new("RGB", (1920, 1080)).save(res / "M81.jpg")
    for i in range(10):
        name = f"Light_M 81_10.0s_IRCUT_20260328-2245{i:02d}"
        hdu = fits.PrimaryHDU(_seestar_frame(rng, shift=(rng.integers(-6, 6), rng.integers(-6, 6))))
        hdu.header["BAYERPAT"] = "GRBG"
        hdu.header["EXPTIME"] = 10.0
        hdu.header["INSTRUME"] = "Seestar S50"
        hdu.writeto(sub / f"{name}.fit")
        Image.new("RGB", (1920, 1080)).save(sub / f"{name}.jpg")
        Image.new("RGB", (192, 108)).save(sub / f"{name}_thn.jpg")

    found = ss.find_sessions(str(works))
    assert [os.path.basename(p) for p in found] == ["M81_sub"]          # not M81 too
    assert ss.session_name(str(sub)) == "M81"

    r = subprocess.run([sys.executable, os.path.join(ROOT, "starstack.py"), str(works), "-j", "2"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "debayering as GRBG" in r.stdout
    assert "10 files that are thumbnails" in r.stdout        # _thn.jpg removed by name, as one line
    assert "10 files that are JPEGs next to real frames" in r.stdout
    assert "10 actual subs" in r.stdout
    assert "integration   1m 40s" in r.stdout               # 10 x 10 s
    out = works / "stacks" / "M81.tif"
    assert out.exists()
    import tifffile
    img = tifffile.imread(str(out)).astype(np.float32)
    assert img.shape == (1080, 1920, 3)
    r_med, b_med = np.median(img[..., 0]), np.median(img[..., 2])
    assert r_med > b_med * 1.2, f"colour swapped? R={r_med} B={b_med}"   # GRBG honoured, not RGGB


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
