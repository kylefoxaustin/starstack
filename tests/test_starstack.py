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


def _odyssey_frame(rng, H=1094, W=1452, shift=(0, 0)):
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


def _scopepull_observation(d, rng, n, target):
    """What scopepull's ingest writes: frames/ (TIFF + FITS twins),
    calibration/ (dark, twins too), reference/ (StackSum + preview.jpg),
    observation.json with the scope manifest inside."""
    import json
    import tifffile
    from astropy.io import fits
    from PIL import Image
    for sub in ("frames", "calibration", "reference"):
        (d / sub).mkdir(parents=True)
    for i in range(n):
        stem = d / "frames" / f"20260131T0{i:02d}_StackInput"
        fr = _odyssey_frame(rng, shift=(rng.integers(-5, 5), rng.integers(-5, 5)))
        tifffile.imwrite(str(stem) + ".tiff", fr)
        h = fits.PrimaryHDU(fr)
        h.header["BAYERPAT"] = "RGGB"; h.header["SENSPAT"] = "GBRG"; h.header["EXPTIME"] = 4.0
        h.writeto(str(stem) + ".fits")
    dark = (rng.normal(2000, 30, (1094, 1452))).clip(0, 65535).astype(np.uint16)
    tifffile.imwrite(str(d / "calibration" / "20260131T000_DarkframeMean.tiff"), dark)
    fits.PrimaryHDU(dark).writeto(str(d / "calibration" / "20260131T000_DarkframeMean.fits"))
    tifffile.imwrite(str(d / "reference" / "20260131T999_StackSum.tiff"),
                     np.zeros((1088, 1452), np.uint16))          # the Siril-breaker
    Image.new("RGB", (1452, 1094)).save(d / "reference" / "preview.jpg")
    (d / "observation.json").write_text(json.dumps({
        "scope_manifest": {"nameTarget": target, "expo": 3970000, "gain": 20,
                           "sensor": "IMX415", "obs_attr": {"frames_saved": n, "frames_stacked": n - 1}},
        "catalog": {}, "pulled_at": "2026-02-01T05:00:00+00:00", "bayer_pattern": "RGGB"}))
    (d / "SHA256SUMS").write_text("")


def test_scopepull_archive_layout(tmp_path):
    """A scopepull archive is <root>/<date>/<observation>/. Pointed at the
    root it stacks every observation of every night; each observation is
    lights from frames/ (FITS, not the TIFF twins), the dark from
    calibration/, nothing from reference/, named from observation.json."""
    rng = np.random.default_rng(5)
    root = tmp_path / "odyssey"
    _scopepull_observation(root / "2026-01-31" / "m81-bode-s-galaxy__38102043", rng, 8, "M81 - Bode's Galaxy")
    _scopepull_observation(root / "2026-02-01" / "m27__38102099", rng, 6, "M27 - Dumbbell Nebula")
    (root / "2026-02-01" / "m13__38102100.partial" / "frames").mkdir(parents=True)   # mid-pull
    (root / "2026-02-01" / "m13__38102100.partial" / "frames" / "x_StackInput.tiff").write_bytes(b"junk")

    one = str(root / "2026-01-31" / "m81-bode-s-galaxy__38102043")
    assert set(ss.layout_dirs(one)) == {"light", "dark"}            # reference/ is not a kind
    assert ss.session_name(one) == "M81 - Bode's Galaxy"
    night = ss.find_sessions(str(root / "2026-02-01"))
    assert [os.path.basename(p) for p in night] == ["m27__38102099"]   # .partial skipped
    whole = ss.find_sessions(str(root))
    assert [os.path.basename(p) for p in whole] == ["m81-bode-s-galaxy__38102043", "m27__38102099"]
    kept, dropped = ss.prefer_fits(sorted(str(p) for p in (root / "2026-01-31" / "m81-bode-s-galaxy__38102043" / "frames").iterdir()))
    assert dropped == 8 and all(p.endswith(".fits") for p in kept)

    r = subprocess.run([sys.executable, os.path.join(ROOT, "starstack.py"), str(root), "-j", "2"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    log = r.stdout
    assert "whole night: 2 session folders" in log
    assert "scopepull archive: frames/, calibration/" in log or "scopepull archive: calibration/, frames/" in log
    assert "9 frames come as both TIFF and FITS" in log            # 8 lights + 1 dark, first session
    assert "scopepull pull: M81 - Bode's Galaxy  8 x 4.0s, gain 20, IMX415" in log
    assert "8 lights, 1 dark" in log
    assert "debayering as RGGB" in log
    assert "StackSum" not in log and "preview" not in log          # reference/ never entered the pile
    assert "2/2 sessions stacked" in log
    out = root / "stacks" / "M81 - Bode's Galaxy.tif"
    assert out.exists() and (root / "stacks" / "M27 - Dumbbell Nebula.tif").exists()
    import tifffile
    img = tifffile.imread(str(out)).astype(np.float32)
    assert img.shape == (1094, 1452, 3)
    assert np.median(img[..., 0]) > np.median(img[..., 2]) * 1.2    # RGGB honoured: R > B

    # a re-run must not mistake stacks/ (which now holds TIFFs) for a session
    assert [os.path.basename(p) for p in ss.find_sessions(str(root))] == \
        ["m81-bode-s-galaxy__38102043", "m27__38102099"]


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


# ------------------------------------------------- scopepull --pull --------
def test_run_scopepull_missing_command(monkeypatch):
    """No scopepull on PATH -> a helpful message and None (not a crash)."""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)
    msgs = []
    dest = ss.run_scopepull(None, None, lambda *a: msgs.append(" ".join(map(str, a))))
    assert dest is None
    assert any("scopepull" in m for m in msgs)


def test_run_scopepull_builds_command_and_returns_dest(monkeypatch, tmp_path):
    import shutil
    import subprocess
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/scopepull")
    seen = {}

    class _R:
        returncode = 0

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    dest = ss.run_scopepull(str(tmp_path), "M81", lambda *a: None)
    assert dest == str(tmp_path)
    assert seen["cmd"] == [
        "/usr/bin/scopepull", "pull", "--new", "--dest", str(tmp_path), "--target", "M81",
    ]


def test_run_scopepull_default_dest_when_no_folder(monkeypatch):
    import os
    import shutil
    import subprocess
    monkeypatch.setattr(shutil, "which", lambda name: "scopepull")
    seen = {}

    class _R:
        returncode = 2  # nothing new -> still stack what's there

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    dest = ss.run_scopepull(None, None, lambda *a: None)
    assert dest == os.path.join(os.path.expanduser("~"), "Astro", "odyssey")
    assert "--target" not in seen["cmd"]


def test_run_scopepull_unreachable_returns_none(monkeypatch, tmp_path):
    import shutil
    import subprocess
    monkeypatch.setattr(shutil, "which", lambda name: "scopepull")

    class _R:
        returncode = 3  # scope unreachable

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _R())
    assert ss.run_scopepull(str(tmp_path), None, lambda *a: None) is None
