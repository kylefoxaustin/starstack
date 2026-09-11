#!/usr/bin/env python3
"""
starstack -- dump a folder of frames in, get one stacked image out.

    python starstack.py /path/to/frames -o result.tif

It figures out the rest: reads TIFF/FITS/PNG/JPG, finds the dark frames by
name and builds a master dark, does not care if some frames are a different
size (star alignment handles that), skips unreadable frames instead of dying
on them, debayers if the frames are raw Bayer, star-aligns everything to a
reference, and combines with a sigma-clipped mean.

No sequence files. No project folder. No state left behind.
"""

from __future__ import annotations

import argparse
import re
import os
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field

import numpy as np
import warnings

__version__ = "0.2.3"

warnings.filterwarnings("ignore")   # astropy is chatty about slightly-off FITS headers

# ----------------------------------------------------------------------------
# optional deps, imported lazily with useful error messages
# ----------------------------------------------------------------------------

def _need(mod: str, pip_name: str | None = None):
    try:
        return __import__(mod)
    except ImportError:
        sys.exit(
            f"starstack needs '{mod}'. Install it with:\n"
            f"    pip install {pip_name or mod}"
        )


IMAGE_EXT = {".tif", ".tiff", ".fit", ".fits", ".fts", ".png", ".jpg", ".jpeg"}
FITS_EXT = {".fit", ".fits", ".fts"}


# ----------------------------------------------------------------------------
# loading
# ----------------------------------------------------------------------------

@dataclass
class Frame:
    path: str
    shape: tuple | None = None          # (H, W) or (H, W, C) after debayer plan
    raw_shape: tuple | None = None      # as stored on disk
    bayer: str | None = None
    bits: int = 0                       # bits per sample on disk
    kind: str = "light"                 # light | dark | skip
    status: str = "pending"             # used | unreadable | align | dark | quality
    note: str = ""
    stars: int = 0                      # per-frame quality metrics (pre-warp)
    fwhm: float = float("nan")
    bg: float = float("nan")
    noise: float = float("nan")
    weight: float = 1.0
    slot: int = -1                      # index in the scratch cube


def _to_float(a: np.ndarray) -> np.ndarray:
    """Bring any integer dtype onto a 0..1 float32 scale; leave floats alone."""
    if np.issubdtype(a.dtype, np.integer):
        info = np.iinfo(a.dtype)
        scale = float(info.max) if info.min >= 0 else float(info.max)
        return (a.astype(np.float32) / scale)
    return a.astype(np.float32, copy=False)


def _normalize_axes(a: np.ndarray) -> np.ndarray:
    """Return (H, W) or (H, W, C). FITS often stores colour as (C, H, W)."""
    a = np.squeeze(a)
    if a.ndim == 3 and a.shape[0] in (3, 4) and a.shape[0] < a.shape[-1]:
        a = np.moveaxis(a, 0, -1)
    if a.ndim == 3 and a.shape[-1] == 4:        # drop alpha
        a = a[..., :3]
    return a


def read_image(path: str, want_header: bool = False):
    """Load any supported file to float32, plus a header dict for FITS."""
    ext = os.path.splitext(path)[1].lower()
    header = {}
    if ext in FITS_EXT:
        from astropy.io import fits as _fits
        with _fits.open(path, memmap=False) as hdul:
            hdu = next((h for h in hdul if getattr(h, "data", None) is not None), None)
            if hdu is None:
                raise ValueError("no image data in FITS")
            data = np.asarray(hdu.data)
            header = {k: hdu.header[k] for k in hdu.header if k}
    elif ext in (".tif", ".tiff"):
        try:
            import tifffile
            data = tifffile.imread(path)
        except Exception:
            # LZW/JPEG-compressed TIFFs need 'imagecodecs' for tifffile;
            # Pillow reads them fine, including 16-bit greyscale
            from PIL import Image
            with Image.open(path) as im:
                data = np.asarray(im)
    else:
        from PIL import Image
        with Image.open(path) as im:
            data = np.asarray(im)

    data = _normalize_axes(data)
    data = _to_float(data)
    if not np.isfinite(data).all():
        data = np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)
    return (data, header) if want_header else data


def probe(path: str):
    """Cheap (shape, bayer, bits-per-sample) read without pulling full pixel
    data where possible."""
    ext = os.path.splitext(path)[1].lower()
    if ext in FITS_EXT:
        from astropy.io import fits as _fits
        with _fits.open(path, memmap=True) as hdul:
            hdu = next((h for h in hdul if getattr(h, "shape", None)), None)
            if hdu is None:
                raise ValueError("no image data in FITS")
            shp = tuple(int(x) for x in hdu.shape)
            if len(shp) == 3 and shp[0] in (3, 4):
                shp = (shp[1], shp[2], shp[0])
            bayer = hdu.header.get("BAYERPAT") or hdu.header.get("COLORTYP")
            if isinstance(bayer, str):
                bayer = bayer.strip().upper()
                if bayer not in ("RGGB", "BGGR", "GRBG", "GBRG"):
                    bayer = None
            return shp, bayer, abs(int(hdu.header.get("BITPIX", 16)))
    if ext in (".tif", ".tiff"):
        import tifffile
        with tifffile.TiffFile(path) as tf:
            s = tf.series[0]
            return tuple(s.shape), None, int(np.dtype(s.dtype).itemsize * 8)
    from PIL import Image
    with Image.open(path) as im:
        w, h = im.size
        nch = len(im.getbands())
        bits = 16 if im.mode.startswith("I;16") else 32 if im.mode in ("I", "F") else 8
        return ((h, w) if nch == 1 else (h, w, min(nch, 3))), None, bits


# ----------------------------------------------------------------------------
# debayer
# ----------------------------------------------------------------------------

def debayer(mono: np.ndarray, pattern: str) -> np.ndarray:
    """Bayer -> RGB. Uses OpenCV when present, bilinear numpy fallback if not."""
    pattern = pattern.upper()
    try:
        import cv2
        code = {
            "RGGB": cv2.COLOR_BAYER_BG2RGB,   # OpenCV names are offset by one row/col
            "BGGR": cv2.COLOR_BAYER_RG2RGB,
            "GRBG": cv2.COLOR_BAYER_GB2RGB,
            "GBRG": cv2.COLOR_BAYER_GR2RGB,
        }[pattern]
        as16 = np.clip(mono, 0, 1)
        as16 = (as16 * 65535.0).astype(np.uint16)
        return cv2.cvtColor(as16, code).astype(np.float32) / 65535.0
    except ImportError:
        pass

    from scipy.ndimage import convolve
    h, w = mono.shape
    masks = {c: np.zeros((h, w), np.float32) for c in "RGB"}
    order = {"RGGB": "RGGB", "BGGR": "BGGR", "GRBG": "GRBG", "GBRG": "GBRG"}[pattern]
    for idx, c in enumerate(order):
        masks[c][idx // 2::2, idx % 2::2] = 1.0
    kg = np.array([[0, 1, 0], [1, 4, 1], [0, 1, 0]], np.float32) / 4.0
    krb = np.array([[1, 2, 1], [2, 4, 2], [1, 2, 1]], np.float32) / 4.0
    out = np.empty((h, w, 3), np.float32)
    for i, c in enumerate("RGB"):
        k = kg if c == "G" else krb
        num = convolve(mono * masks[c], k, mode="mirror")
        den = convolve(masks[c], k, mode="mirror")
        out[..., i] = num / np.maximum(den, 1e-6)
    return out


def detect_bayer(mono: np.ndarray, default: str = "RGGB"):
    """Decide from the pixels whether a 2-D image is an undebayered mosaic.
    TIFFs carry no BAYERPAT header, so this is what smart-scope TIFF exports
    need. Returns a pattern string or None.

    In a mosaic the four 2x2 phases see different colour filters, so their
    means differ, and the two green phases (one diagonal) agree with each
    other far better than with the other two. A mono or already-debayered
    image has four phases that all agree.
    """
    if mono.ndim != 2 or min(mono.shape) < 64:
        return None
    a = mono.astype(np.float32)
    a = np.minimum(a, np.percentile(a, 98))         # stars out of the statistics
    ph = {(0, 0): a[0::2, 0::2].mean(), (0, 1): a[0::2, 1::2].mean(),
          (1, 0): a[1::2, 0::2].mean(), (1, 1): a[1::2, 1::2].mean()}
    vals = np.array(list(ph.values()))
    spread = vals.max() - vals.min()
    if vals.mean() <= 0 or spread / vals.mean() < 0.03:  # real mosaics show 20-40%
        return None
    d_main = abs(ph[(0, 0)] - ph[(1, 1)])
    d_anti = abs(ph[(0, 1)] - ph[(1, 0)])
    if min(d_main, d_anti) > 0.5 * spread:            # no matching pair -> not a mosaic
        return None
    # Greens sit on the diagonal whose two phases agree best. Which of the other
    # two is red is not knowable from statistics, so: greens on the anti-diagonal
    # is RGGB (Unistellar / Seestar native); greens on the main diagonal is what
    # RGGB becomes after the vertical flip Siril applies on TIFF import, GBRG.
    if d_anti <= d_main:
        return default if default in ("RGGB", "BGGR") else "RGGB"
    return default if default in ("GRBG", "GBRG") else "GBRG"


# ----------------------------------------------------------------------------
# alignment helpers
# ----------------------------------------------------------------------------

def luminance(a: np.ndarray) -> np.ndarray:
    if a.ndim == 2:
        return a
    return (0.2126 * a[..., 0] + 0.7152 * a[..., 1] + 0.0722 * a[..., 2]).astype(np.float32)


def star_count(lum: np.ndarray) -> int:
    """Rough star count -- only used to pick a good reference frame."""
    import astroalign as aa
    try:
        return len(aa._find_sources(lum.astype(np.float32)))
    except Exception:
        med = np.median(lum)
        mad = np.median(np.abs(lum - med)) + 1e-9
        return int(((lum - med) > 8 * 1.4826 * mad).sum())


def frame_metrics(lum: np.ndarray) -> dict:
    """Per-frame quality: background, noise, star count and a median FWHM in
    pixels from intensity-weighted second moments of star blobs. Cheap, and
    it's what decides which frames are dragging the stack down."""
    from scipy import ndimage as ndi
    a = np.nan_to_num(lum.astype(np.float32))
    samp = a.reshape(-1)[::5]
    bg = float(np.median(samp))
    noise = float(np.median(np.abs(samp - bg)) * 1.4826) + 1e-9
    mask = a > bg + 5.0 * noise
    lab, n = ndi.label(mask)
    if n == 0:
        return {"stars": 0, "fwhm": float("nan"), "bg": bg, "noise": noise}
    idx = np.arange(1, n + 1)
    area = ndi.sum(mask, lab, idx)
    peak = ndi.maximum(a, lab, idx)
    top = float(a.max())
    ok = (area >= 4) & (area <= 400)
    if top > 0:
        ok &= peak < 0.98 * top                    # saturated stars lie about their width
    sel = idx[ok]
    if sel.size < 3:
        return {"stars": int(sel.size), "fwhm": float("nan"), "bg": bg, "noise": noise}
    # intensity-weighted second moments per blob -> sigma -> FWHM,
    # computed inside each blob's bounding box only (whole-frame index
    # arrays cost more than the rest of the pipeline)
    objs = ndi.find_objects(lab)
    sig = []
    for i in sel:
        sl = objs[i - 1]
        if sl is None:
            continue
        sub = a[sl]; m = (lab[sl] == i)
        w = np.where(m, sub - bg, 0.0).astype(np.float64)
        s0 = w.sum()
        if s0 <= 0:
            continue
        yy, xx = np.mgrid[sl[0], sl[1]]
        mx = (w * xx).sum() / s0; my = (w * yy).sum() / s0
        vx = (w * xx * xx).sum() / s0 - mx ** 2
        vy = (w * yy * yy).sum() / s0 - my ** 2
        sig.append(np.sqrt(max((vx + vy) / 2.0, 1e-6)))
    if len(sig) < 3:
        return {"stars": int(sel.size), "fwhm": float("nan"), "bg": bg, "noise": noise}
    sig = np.array(sig)
    fwhm = float(np.median(2.3548 * sig))
    return {"stars": int(sel.size), "fwhm": fwhm, "bg": bg, "noise": noise}


def assess_quality(frames: list, log, keep_all: bool, use_weights: bool):
    """Decide which registered frames are dragging the stack down, and how
    much the survivors should count. All thresholds are relative to the
    session itself -- no absolute numbers to tune."""
    have = [f for f in frames if np.isfinite(f.fwhm) and f.stars >= 3]
    if len(have) < 10:
        log("  quality check: too few frames to judge anyone. Everyone counts equally.")
        return
    fw = np.array([f.fwhm for f in have]); st = np.array([f.stars for f in have], float)
    bg = np.array([f.bg for f in have]); nz = np.array([f.noise for f in have])
    def rob(x):
        m = float(np.median(x)); return m, float(np.median(np.abs(x - m)) * 1.4826) + 1e-9
    fw_m, fw_s = rob(fw); st_m, _ = rob(st); bg_m, bg_s = rob(bg); nz_m, _ = rob(nz)

    reasons = {}
    if not keep_all:
        for f in have:
            if f.fwhm > fw_m + 2.5 * fw_s and f.fwhm > 1.15 * fw_m:
                reasons[f.path] = ("blurry", f"blurry: FWHM {f.fwhm:.1f} px vs {fw_m:.1f} median")
            elif f.stars < 0.5 * st_m:
                reasons[f.path] = ("starved", f"starved: {f.stars} stars vs {st_m:.0f} median")
            elif f.bg > bg_m + max(3.0 * bg_s, 0.03 * bg_m):     # 3 MADs, and at least 3% brighter
                reasons[f.path] = ("cloudy", f"cloudy/bright: background {f.bg:.4f} vs {bg_m:.4f} median")
        # never throw away more than a quarter of the session on a bad night
        cap = int(0.25 * len(have))
        if len(reasons) > cap:
            worst = sorted(reasons, key=lambda p: -next(f.fwhm for f in have if f.path == p))[:cap]
            reasons = {p: reasons[p] for p in worst}
        for f in have:
            if f.path in reasons:
                f.status, f.note = "quality", reasons[f.path][1]
    kept = [f for f in have if f.status == "used"] + [f for f in frames if f not in have and f.status == "used"]

    if use_weights:
        for f in kept:
            if np.isfinite(f.fwhm) and f.noise > 0:
                f.weight = float(np.clip((fw_m / f.fwhm) ** 2 * (nz_m / f.noise) ** 2, 0.2, 3.0))
            else:
                f.weight = 1.0
        ws = np.array([f.weight for f in kept]); ws /= ws.mean()
        for f, w in zip(kept, ws):
            f.weight = float(w)

    n_drop = len(reasons)
    tally = Counter(r[0] for r in reasons.values())
    line = f"  quality check: FWHM median {fw_m:.1f} px, {st_m:.0f} stars per frame."
    if n_drop:
        parts = ", ".join(f"{v} {k}" for k, v in tally.most_common())
        line += f" Dropped {n_drop} -- {parts}. They know what they did."
    else:
        line += " Nobody dropped. Suspiciously well-behaved."
    log(line)
    if use_weights and kept:
        ws = [f.weight for f in kept]
        log(f"  weighting the rest by sharpness and noise ({min(ws):.1f}x to {max(ws):.1f}x).")


def match_levels(img: np.ndarray, ref_med: float, ref_scale: float) -> np.ndarray:
    """Additive + multiplicative normalization so frames combine cleanly.
    Statistics come from the LUMINANCE, same as the reference's, and one
    transform is applied to all channels -- measuring across R, G and B at
    once would count the colour separation as 'spread' and squash it."""
    sample = luminance(img).reshape(-1)[::7]
    sample = sample[np.isfinite(sample)]
    if sample.size == 0:
        return img
    med = float(np.median(sample))
    mad = float(np.median(np.abs(sample - med))) + 1e-9
    return ((img - med) * (ref_scale / mad) + ref_med).astype(np.float32)


def warp_to_ref(tform, img: np.ndarray, ref_hw: tuple) -> np.ndarray:
    """Apply an astroalign transform. OpenCV when available (fast, all channels
    at once), astroalign's own skimage path otherwise. Off-frame pixels -> NaN."""
    h, w = ref_hw
    try:
        import cv2
        M = np.asarray(tform.params, dtype=np.float64)[:2]
        src = np.nan_to_num(img.astype(np.float32))
        out = cv2.warpAffine(src, M, (w, h), flags=cv2.INTER_LANCZOS4,
                             borderMode=cv2.BORDER_CONSTANT, borderValue=float("nan"))
        ones = np.ones(img.shape[:2], np.float32)
        fp = cv2.warpAffine(ones, M, (w, h), flags=cv2.INTER_NEAREST,
                            borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        if out.ndim == 3:
            out[fp < 0.5] = np.nan
        else:
            out[fp < 0.5] = np.nan
        return out
    except ImportError:
        import astroalign as aa
        ref = np.zeros((h, w), np.float32)
        if img.ndim == 2:
            r, fp = aa.apply_transform(tform, img, ref)
            return np.where(fp, np.nan, r).astype(np.float32)
        chans = []
        for c in range(img.shape[-1]):
            r, fp = aa.apply_transform(tform, img[..., c], ref)
            chans.append(np.where(fp, np.nan, r))
        return np.stack(chans, axis=-1).astype(np.float32)


def crop_to(a: np.ndarray, hw: tuple) -> np.ndarray:
    """Top-left anchored crop/NaN-pad to (H, W). Cameras truncate at the bottom,
    so anchoring top-left keeps the good rows lined up."""
    h, w = hw
    out_shape = (h, w) + a.shape[2:]
    out = np.full(out_shape, np.nan, np.float32)
    hh, ww = min(h, a.shape[0]), min(w, a.shape[1])
    out[:hh, :ww] = a[:hh, :ww]
    return out


DARK_WORDS = ("darkframe", "dark_", "_dark", "-dark", "dark-", "darks", "masterdark")


def looks_like_dark(path: str) -> bool:
    name = os.path.basename(path).lower()
    return any(w in name for w in DARK_WORDS) or name.startswith("dark")


def build_master_dark(darks: list, log) -> np.ndarray | None:
    """Median-combine dark frames grouped by size; returns the biggest group."""
    if not darks:
        return None
    by_shape = {}
    for f in darks:
        by_shape.setdefault(f.raw_shape, []).append(f)
    shape, group = max(by_shape.items(), key=lambda kv: len(kv[1]))
    if len(by_shape) > 1:
        log(f"  darks come in {len(by_shape)} sizes; using the {len(group)} at "
            f"{shape[1]}x{shape[0]}")
    imgs = []
    for f in group[:200]:
        try:
            imgs.append(read_image(f.path))
            f.status = "dark"
        except Exception as e:
            f.status, f.note = "unreadable", str(e)[:80]
    if not imgs:
        return None
    cube = np.stack(imgs, axis=0)
    master = np.median(cube, axis=0).astype(np.float32)
    log(f"master dark: {len(imgs)} frame{'s' if len(imgs) != 1 else ''}, median {float(np.median(master)):.5f}. Subtracting it from everyone.")
    return master


# ----------------------------------------------------------------------------
# per-frame worker (runs in a process pool)
# ----------------------------------------------------------------------------

_W = {}   # worker-global state set by _worker_init


def _worker_init(master_dark, pattern, ref_lum, ref_med, ref_scale, align, normalize):
    _W.update(master_dark=master_dark, pattern=pattern, ref_lum=ref_lum,
              ref_med=ref_med, ref_scale=ref_scale, align=align, normalize=normalize)


def _prepare(path):
    img = read_image(path)
    md = _W["master_dark"]
    if md is not None and img.ndim == md.ndim:
        h = min(img.shape[0], md.shape[0]); w = min(img.shape[1], md.shape[1])
        img = img.copy(); img[:h, :w] -= md[:h, :w]
    if _W["pattern"] and img.ndim == 2:
        img = debayer(img, _W["pattern"])
    return img


def _process_one(args):
    idx, path, is_ref = args
    try:
        img = _prepare(path)
    except Exception as e:
        return idx, "unreadable", str(e)[:80], None, None
    ref_lum = _W["ref_lum"]
    note = ""
    lum = luminance(img)
    try:
        metrics = frame_metrics(lum)          # measured before warping/normalizing
    except Exception:
        metrics = None
    if not is_ref:
        if _W["align"]:
            try:
                import astroalign as aa
                tform, _ = aa.find_transform(lum, ref_lum)
                img = warp_to_ref(tform, img, ref_lum.shape)
            except Exception as e:
                return idx, "align", str(e)[:80], None, metrics
        elif img.shape[:2] != ref_lum.shape:
            note = f"resized from {img.shape[1]}x{img.shape[0]}"
            img = crop_to(img, ref_lum.shape)
    if _W["normalize"]:
        img = match_levels(img, _W["ref_med"], _W["ref_scale"])
    return idx, "used", note, img.astype(np.float32), metrics


# ----------------------------------------------------------------------------
# stacking
# ----------------------------------------------------------------------------

def sigma_clip_stack(mm: np.memmap, slots: list, weights: np.ndarray,
                     sigma: float, iters: int, chunk_rows: int, progress=None):
    """NaN-aware, weighted, sigma-clipped mean over the chosen slots of a
    memmapped (N, H, W[, C]) cube. Clipping decides which pixels count;
    weights decide how much."""
    shape = mm.shape[1:]
    out = np.zeros(shape, np.float32)
    cov = np.zeros(shape[:2], np.uint16)
    h = shape[0]
    slots = np.asarray(slots)
    wshape = (len(slots),) + (1,) * len(shape)
    wcol = np.asarray(weights, np.float32).reshape(wshape)
    for y0 in range(0, h, chunk_rows):
        y1 = min(y0 + chunk_rows, h)
        if progress:
            progress(y0, h)
        block = np.array(mm[slots, y0:y1], dtype=np.float32)
        keep = np.isfinite(block)
        for _ in range(iters):
            work = np.where(keep, block, np.nan)
            with np.errstate(all="ignore"):
                med = np.nanmedian(work, axis=0)
                mad = np.nanmedian(np.abs(work - med), axis=0) * 1.4826
            lo, hi = med - sigma * mad, med + sigma * mad
            newkeep = keep & (block >= lo) & (block <= hi)
            # never clip a pixel down to nothing
            empty = newkeep.sum(axis=0) == 0
            newkeep[:, empty] = keep[:, empty]
            if np.array_equal(newkeep, keep):
                break
            keep = newkeep
        with np.errstate(all="ignore"):
            wk = keep * wcol
            wsum = wk.sum(axis=0)
            summed = (np.where(keep, block, 0.0) * wcol).sum(axis=0)
            out[y0:y1] = np.where(wsum > 0, summed / np.where(wsum > 0, wsum, 1), np.nan)
        cov[y0:y1] = (np.isfinite(block).sum(axis=0) if block.ndim == 3
                      else np.isfinite(block[..., 0]).sum(axis=0))
    return out, cov


def autostretch(img: np.ndarray, shadow_clip: float = -2.8, target_bg: float = 0.25):
    """Siril-style midtone transfer autostretch, for the preview only.
    Colour images also get a neutral background (raw OSC data is green)."""
    x = np.nan_to_num(img.astype(np.float32))
    if x.ndim == 3:
        meds = np.array([np.median(x[..., c]) for c in range(3)])
        x = x - meds + meds.mean()
    lum = luminance(x)
    med = float(np.median(lum))
    mad = float(np.median(np.abs(lum - med))) * 1.4826 + 1e-9
    lo = max(0.0, med + shadow_clip * mad)
    hi = float(np.percentile(lum, 99.995))
    if hi <= lo:
        hi = lo + 1e-6
    y = np.clip((x - lo) / (hi - lo), 0, 1)
    m = np.clip((med - lo) / (hi - lo), 1e-6, 1 - 1e-6)
    # midtone transfer function
    def mtf(v, m):
        return ((m - 1) * v) / ((2 * m - 1) * v - m)
    mid = mtf(np.full(1, m, np.float32), target_bg)[0]
    return mtf(y, np.clip(mid, 1e-6, 1 - 1e-6))


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def run_scopepull(folder, target, log):
    """Fetch new observations off the scope with scopepull, then return the
    archive folder to stack (or None on a hard failure). Shells out to the
    `scopepull` command so starstack has no hard dependency on it for plain
    stacking. scopepull deposits one subfolder per observation, which is
    exactly what whole-night mode already knows how to stack.
    """
    import shutil
    import subprocess

    exe = shutil.which("scopepull")
    if not exe:
        log("owl wants to fetch from the scope, but scopepull isn't installed.")
        log("  install:  uv tool install scopepull   (or)   pipx install scopepull")
        log("  then:     starstack --pull")
        log("  docs:     https://github.com/kylefoxaustin/scopepull")
        return None

    dest = folder or os.path.join(os.path.expanduser("~"), "Astro", "odyssey")
    cmd = [exe, "pull", "--new", "--dest", dest]
    if target:
        cmd += ["--target", target]
    log(f"owl is on the scope:  {' '.join(cmd)}")
    try:
        rc = subprocess.run(cmd).returncode
    except OSError as e:
        log(f"couldn't run scopepull: {e}")
        return None

    # scopepull exit codes: 0 ok, 2 nothing new, 3 unreachable, 4 DDD off, 5 partial.
    if rc == 3:
        log("scope unreachable -- are you on its Wi-Fi (Odyssey-xxxx)?")
        return None
    if rc == 4:
        log("Direct Data Download is off -- enable it in the Unistellar app, then retry.")
        return None
    if rc == 2:
        log("nothing new on the scope; stacking what's already in the archive.")
    elif rc == 5:
        log("some observations didn't finish (they'll retry next --pull); stacking the rest.")
    return dest


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="starstack",
        description="Point it at a folder of frames. Push the button. Get one stacked image out. "
                    "No sequence files, no process folder, no opinions about your workflow.",
    )
    p.add_argument("--version", action="version", version=f"starstack {__version__}")
    p.add_argument("folder", nargs="?", default=None,
                   help="folder of frames (or a glob). Optional with --pull.")
    p.add_argument("--pull", action="store_true",
                   help="fetch new observations off the scope with scopepull, then stack them "
                        "(needs scopepull: uv tool install scopepull)")
    p.add_argument("--pull-target", default=None, metavar="TEXT",
                   help="with --pull: only fetch observations whose target matches TEXT")
    p.add_argument("-o", "--out", default="stacked.tif",
                   help="output file; .tif/.tiff or .fit/.fits (default: stacked.tif)")
    p.add_argument("--method", choices=["sigma", "mean", "median"], default="sigma")
    p.add_argument("--sigma", type=float, default=3.0, help="clip threshold (default 3.0)")
    p.add_argument("--iters", type=int, default=3, help="sigma-clip iterations")
    p.add_argument("--debayer", default="auto",
                   help="auto | none | RGGB | BGGR | GRBG | GBRG  (Unistellar/Seestar = RGGB)")
    p.add_argument("--no-align", action="store_true", help="skip star registration")
    p.add_argument("--no-normalize", action="store_true", help="skip level matching")
    p.add_argument("--ref", type=int, default=None, help="reference frame index (default: auto)")
    p.add_argument("--darks", default=None,
                   help="folder or glob of dark frames (default: any file with 'dark' in its name)")
    p.add_argument("--no-darks", action="store_true", help="ignore dark frames entirely")
    p.add_argument("--sort", action="store_true",
                   help="don't stack: sort a one-pile folder into lights/ darks/ flats/ bias/ extras/ and stop")
    p.add_argument("--dry-run", action="store_true", help="with --sort: show the plan, move nothing")
    p.add_argument("--keep-all", action="store_true",
                   help="skip the quality pass: no frame is dropped for being blurry, cloudy or starved")
    p.add_argument("--no-weights", action="store_true",
                   help="every kept frame counts equally instead of by sharpness and noise")
    p.add_argument("--max-frames", type=int, default=None)
    p.add_argument("--min-stars", type=int, default=8,
                   help="frames with fewer detected stars are not used as reference")
    p.add_argument("--bits", type=int, choices=[16, 32], default=32,
                   help="output bit depth: 32 = float (default), 16 = integer")
    p.add_argument("--preview", default=None, help="also write a stretched PNG here")
    p.add_argument("--report", default=None, help="write a per-frame CSV report here")
    p.add_argument("-j", "--jobs", type=int, default=None,
                   help="parallel workers (default: CPU count, max 8)")
    p.add_argument("--scratch", default=None, metavar="DIR",
                   help="where the temporary scratch cube goes (default: the system temp folder)")
    p.add_argument("--no-log", action="store_true", help="don't write a .log next to the output")
    p.add_argument("-q", "--quiet", action="store_true")
    args = p.parse_args(argv)
    log = (lambda *a: None) if args.quiet else (lambda *a: print(*a, flush=True))

    if args.pull:
        dest = run_scopepull(args.folder, args.pull_target, log)
        if dest is None:
            return 3
        args.folder = dest
    if args.folder is None:
        p.error("a folder is required (or use --pull to fetch from the scope first)")

    if args.sort:
        sessions = find_sessions(args.folder)
        if sessions:
            rc = 0
            for sess in sessions:
                rc |= sort_folder(sess, args.dry_run, log)
            return rc
        return sort_folder(args.folder, args.dry_run, log)

    sessions = find_sessions(args.folder)
    if sessions:
        return run_batch(args, sessions)
    return run(args)


def _has_images(folder: str) -> bool:
    try:
        return any(os.path.splitext(f)[1].lower() in IMAGE_EXT for f in os.listdir(folder))
    except OSError:
        return False


LAYOUT_DIRS = {"lights": "light", "light": "light", "darks": "dark", "dark": "dark",
               "flats": "flat", "flat": "flat", "bias": "bias", "biases": "bias",
               "offset": "bias", "darkflats": "darkflat", "dark_flats": "darkflat",
               "flat_darks": "darkflat", "flatdarks": "darkflat",
               # scopepull archive (github.com/kylefoxaustin/scopepull): one
               # observation = frames/ calibration/ reference/ observation.json.
               # reference/ holds the scope's own StackSum and preview.jpg --
               # deliberately not in this table, so it is never read.
               "frames": "light", "calibration": "dark"}
FLAT_WORDS = ("flat",)
BIAS_WORDS = ("bias", "offset")
EXTRA_WORDS = ("preview", "thumb", "_thn", "stacksum", "stacked", "final")


def layout_dirs(folder: str) -> dict:
    """Seestar-style layout: {kind: subfolder} for lights/darks/flats/bias
    subfolders that exist and hold images. Empty dict if it isn't one."""
    found = {}
    try:
        for d in os.listdir(folder):
            kind = LAYOUT_DIRS.get(d.lower())
            p = os.path.join(folder, d)
            if kind and os.path.isdir(p) and _has_images(p):
                found[kind] = p
    except OSError:
        pass
    return found if "light" in found else {}


def prefer_fits(paths: list) -> tuple:
    """scopepull writes every frame twice: the scope's TIFF verbatim and a
    FITS of the same pixels with the headers filled in (BAYERPAT, EXPTIME,
    DATE-OBS). Stacking both would count each frame twice. When a TIFF and a
    FITS share a stem, keep the FITS -- it knows things the TIFF can't.
    Returns (kept paths, number of TIFFs set aside)."""
    stems = {}
    for p in paths:
        stem, ext = os.path.splitext(p)
        stems.setdefault(stem.lower(), {})[ext.lower()] = p
    kept, dropped = [], 0
    for p in paths:
        stem, ext = os.path.splitext(p)
        ext = ext.lower()
        twins = stems[stem.lower()]
        if ext in (".tif", ".tiff") and any(e in twins for e in FITS_EXT):
            dropped += 1
            continue
        kept.append(p)
    return kept, dropped


def read_manifest(folder: str) -> dict:
    """The scope's own description of a session, if it left one.
    Unistellar: manifest.json next to the frames. scopepull: observation.json
    with the same manifest tucked under "scope_manifest". {} if neither."""
    import json
    for name, key in (("manifest.json", None), ("observation.json", "scope_manifest")):
        p = os.path.join(folder, name)
        if os.path.exists(p):
            try:
                with open(p) as fh:
                    mf = json.load(fh)
                mf = mf.get(key) if key else mf
                if isinstance(mf, dict):
                    mf["_source"] = name
                    return mf
            except Exception:
                pass
    return {}


def classify_name(path: str) -> str:
    """What a frame is, judging only by its filename: light | dark | flat |
    bias | darkflat | extra. Used by --sort and by the reader."""
    n = os.path.basename(path).lower()
    if any(w in n for w in EXTRA_WORDS):
        return "extra"
    is_dark = looks_like_dark(path)
    is_flat = any(w in n for w in FLAT_WORDS)
    if is_dark and is_flat:
        return "darkflat"
    if any(w in n for w in BIAS_WORDS):
        return "bias"
    if is_dark:
        return "dark"
    if is_flat:
        return "flat"
    return "light"


def sort_folder(folder: str, dry_run: bool, log) -> int:
    """Move a one-pile scope dump into lights/ darks/ flats/ bias/ extras/
    subfolders -- the layout every stacker understands. manifest.json and
    anything that isn't an image stay where they are."""
    if layout_dirs(folder):
        log("already sorted. Nothing to do. You're welcome anyway.")
        return 0
    names = sorted(f for f in os.listdir(folder)
                   if os.path.splitext(f)[1].lower() in IMAGE_EXT
                   and os.path.isfile(os.path.join(folder, f)))
    if not names:
        sys.exit(f"no image files in {folder}. Nothing to sort.")
    dest_for = {"light": "lights", "dark": "darks", "flat": "flats", "bias": "bias",
                "darkflat": "darkflats", "extra": "extras"}
    plan = {}
    have_raw = any(os.path.splitext(n)[1].lower() in (FITS_EXT | {".tif", ".tiff"}) for n in names)
    for n in names:
        kind = classify_name(n)
        if kind == "light" and have_raw and n.lower().endswith((".jpg", ".jpeg")):
            kind = "extra"                          # a JPEG next to raw frames is a preview
        plan.setdefault(dest_for[kind], []).append(n)
    log(f"sorting {folder}" + (" (dry run -- nothing moves)" if dry_run else ""))
    for sub in ("lights", "darks", "flats", "darkflats", "bias", "extras"):
        if sub in plan:
            ex = plan[sub]
            log(f"  {sub:<10} {len(ex):>5}   e.g. {ex[0]}" + (f" .. {ex[-1]}" if len(ex) > 1 else ""))
    if "lights" not in plan:
        log("  no lights in here. That would be a very short stack. Not sorting.")
        return 1
    if dry_run:
        log("dry run. Run again without --dry-run to move them.")
        return 0
    moved = 0
    for sub, files in plan.items():
        d = os.path.join(folder, sub)
        os.makedirs(d, exist_ok=True)
        for n in files:
            src, dst = os.path.join(folder, n), os.path.join(d, n)
            if os.path.exists(dst):
                log(f"  {n}: already in {sub}/. Left alone.")
                continue
            os.replace(src, dst)
            moved += 1
    log(f"moved {moved} files into {', '.join(sorted(plan))}. "
        f"The stragglers (manifest, csv, whatever) stayed put. Point any stacker at it now.")
    return 0


def seestar_subs(folder: str):
    """Seestar keeps a target's results in `M81/` and its sub-frames next door
    in `M81_sub/`. Given either, return the `_sub` folder if it exists and
    holds images; else None."""
    folder = folder.rstrip("/\\")
    cand = folder if folder.lower().endswith("_sub") else folder + "_sub"
    return cand if os.path.isdir(cand) and _has_images(cand) else None


def find_sessions(folder: str, _deeper: bool = True):
    """Whole-night mode: a folder that holds no frames itself but has
    subfolders that do (an Odyssey `unistellar_observations` download, a
    Seestar MyWorks folder, a night of sorted targets, a scopepull archive
    or one night of it). Returns the session folders, or []."""
    if any(ch in folder for ch in "*?[") or not os.path.isdir(folder) or _has_images(folder):
        return []
    if layout_dirs(folder):                     # lights/ darks/ ... = one session
        return []
    subs = sorted(os.path.join(folder, d) for d in os.listdir(folder)
                  if os.path.isdir(os.path.join(folder, d)))
    out = []
    for d in subs:
        base = os.path.basename(d).lower()
        if base.endswith(".partial"):           # scopepull mid-download; not ours yet
            continue
        if base == "stacks" and os.path.exists(os.path.join(d, "night.log")):
            continue                            # our own output from an earlier run
        if seestar_subs(d) and not d.lower().endswith("_sub"):
            continue                            # `M81/` (finished stacks): its subs are in M81_sub/
        if _has_images(d) or layout_dirs(d):
            out.append(d)
        elif _deeper:
            # a scopepull archive root: <root>/2026-01-31/<observation>/frames/.
            # One level down is a night; a whole archive is a folder of nights.
            out += find_sessions(d, _deeper=False)
    return out


def session_name(folder: str) -> str:
    """Name a session by its target when the scope tells us, else the folder."""
    name = read_manifest(folder).get("nameTarget")
    name = (name or os.path.basename(folder.rstrip("/\\"))).strip()
    if name.lower().endswith("_sub"):           # Seestar: M81_sub -> M81
        name = name[:-4]
    return re.sub(r'[<>:"/\\|?*]+', "-", name).strip(" .") or "stack"


class Logger:
    """Prints (unless quiet) and keeps a copy in a .log file next to the
    output, because "attach the log" is the first thing anyone asks."""

    def __init__(self, quiet: bool, path: str | None):
        self.quiet, self.fh = quiet, None
        if path:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
                self.fh = open(path, "a", encoding="utf-8")
                self.fh.write(f"\n===== starstack {__version__}  {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            except OSError:
                self.fh = None

    def __call__(self, *parts):
        text = " ".join(str(p) for p in parts)
        if not self.quiet:
            print(text, flush=True)
        if self.fh:
            self.fh.write(text + "\n")
            self.fh.flush()

    def close(self):
        if self.fh:
            self.fh.close()
            self.fh = None


def _log_path_for(out_path: str) -> str:
    return os.path.splitext(out_path)[0] + ".log"


def run_batch(args, sessions):
    import copy
    log = Logger(args.quiet, None)
    ext = os.path.splitext(args.out)[1].lower()
    if args.out == "stacked.tif" or ext in IMAGE_EXT:
        out_dir = os.path.join(args.folder, "stacks")
        out_ext = ext if ext in IMAGE_EXT else ".tif"
    else:
        out_dir, out_ext = args.out, ".tif"
    os.makedirs(out_dir, exist_ok=True)
    log = Logger(args.quiet, os.path.join(out_dir, "night.log"))
    # our own output folder from a previous night is not a session
    sessions = [s for s in sessions if os.path.abspath(s) != os.path.abspath(out_dir)]
    if not sessions:
        sys.exit("only found my own previous output in there. Nothing new to stack.")
    parent = os.path.basename(args.folder.rstrip("/\\"))
    log(f"whole night: {len(sessions)} session folders under {parent}. "
        f"Stacking all of them into {out_dir}. Go to bed.")
    names, results, t0 = {}, [], time.time()
    for i, sess in enumerate(sessions, 1):
        name = session_name(sess)
        if name in names:                       # same target twice: keep both
            name = f"{name} ({os.path.basename(sess)})"
        names[name] = sess
        a = copy.copy(args)
        a.folder = sess
        a.out = os.path.join(out_dir, name + out_ext)
        a.preview = os.path.join(out_dir, name + "_look.png")
        a.report = os.path.join(out_dir, name + "_frames.csv") if args.report else None
        log("\n" + "=" * 46 + f"\n[{i}/{len(sessions)}] {name}\n" + "=" * 46)
        try:
            rc = run(a)
            results.append((name, "ok" if rc == 0 else f"exit {rc}", a.out))
        except SystemExit as e:                 # one bad session must not sink the night
            results.append((name, f"failed: {e}", None))
            log(f"  {name}: {e}. Moving on.")
        except Exception as e:
            results.append((name, f"failed: {str(e)[:60]}", None))
            log(f"  {name}: {str(e)[:80]}. Moving on.")
    log("\n" + "-" * 46 + "\n  the night")
    for name, status, out in results:
        log(f"  {name:<32} {status}")
    ok = sum(1 for r in results if r[1] == "ok")
    log(f"  {ok}/{len(results)} sessions stacked in {time.time()-t0:.0f}s  ->  {out_dir}")
    log("-" * 46)
    log("done. all of it. go to bed.")
    log.close()
    return 0 if ok else 1


def run(args):
    log = Logger(args.quiet, _log_path_for(args.out) if not args.no_log else None)
    t0 = time.time()

    # ---- discover -----------------------------------------------------------
    import glob as _glob
    if not any(ch in args.folder for ch in "*?[") and os.path.isdir(args.folder):
        sub = seestar_subs(args.folder)
        if sub and os.path.abspath(sub) != os.path.abspath(args.folder.rstrip("/\\")):
            log(f"that's the Seestar results folder. The actual sub-frames are next door in "
                f"{os.path.basename(sub)}/. Using those.")
            args.folder = sub
    layout = {} if any(ch in args.folder for ch in "*?[") else layout_dirs(args.folder)
    if layout:
        # Seestar-style: lights/ darks/ (flats/ bias/) subfolders
        def _imgs(d):
            return sorted(os.path.join(d, f) for f in os.listdir(d)
                          if os.path.splitext(f)[1].lower() in IMAGE_EXT)
        paths, twins = prefer_fits(_imgs(layout["light"]))
        frames = [Frame(path=p_) for p_ in paths]
        if "dark" in layout and not args.no_darks:
            dpaths, dtwins = prefer_fits(_imgs(layout["dark"]))
            twins += dtwins
            frames += [Frame(path=p_, kind="dark") for p_ in dpaths]
        found = ", ".join(f"{os.path.basename(v)}/" for v in layout.values())
        if "frames" in {os.path.basename(v).lower() for v in layout.values()}:
            log(f"scopepull archive: {found}. reference/ is the scope's own stack; not touching it.")
        else:
            log(f"sorted layout: {found}. Someone raised this scope right.")
        if twins:
            log(f"  {twins} frames come as both TIFF and FITS of the same pixels. "
                f"Using the FITS -- it has the headers. The TIFFs are not being stacked twice.")
        for extra in ("flat", "bias", "darkflat"):
            if extra in layout:
                log(f"  {os.path.basename(layout[extra])}/ noted; not applied yet (flats are next on the list).")
    elif any(ch in args.folder for ch in "*?["):
        paths = sorted(_glob.glob(args.folder))
        frames = [Frame(path=p_) for p_ in paths]
    else:
        paths = sorted(
            os.path.join(args.folder, f)
            for f in os.listdir(args.folder)
            if os.path.splitext(f)[1].lower() in IMAGE_EXT
        )
        paths, twins = prefer_fits(paths)       # pointed straight at scopepull's frames/
        if twins:
            log(f"{twins} frames come as both TIFF and FITS of the same pixels. "
                f"Using the FITS -- it has the headers. The TIFFs are not being stacked twice.")
        frames = [Frame(path=p_) for p_ in paths]
    if not paths:
        sys.exit(f"no image files in {args.folder}. Nothing to stack. Check the path.")

    # darks: by name, plus anything under --darks
    if args.darks:
        if any(ch in args.darks for ch in "*?["):
            dpaths = sorted(_glob.glob(args.darks))
        elif os.path.isdir(args.darks):
            dpaths = sorted(os.path.join(args.darks, f) for f in os.listdir(args.darks)
                            if os.path.splitext(f)[1].lower() in IMAGE_EXT)
        else:
            dpaths = [args.darks]
        known = {f.path for f in frames}
        frames += [Frame(path=p_, kind="dark") for p_ in dpaths if p_ not in known]
        for f in frames:
            if f.path in set(dpaths):
                f.kind = "dark"
    if not args.no_darks:
        for f in frames:
            if f.kind == "light" and looks_like_dark(f.path):
                f.kind = "dark"
    else:
        frames = [f for f in frames if f.kind == "light" and not looks_like_dark(f.path)]

    # a dark in a different file format than the lights came out of some other
    # pipeline (e.g. Siril's master_dark.fit sitting next to camera TIFFs)
    def _family(p_):
        e = os.path.splitext(p_)[1].lower()
        return "tif" if e in (".tif", ".tiff") else "fits" if e in FITS_EXT else e
    light_fams = {_family(f.path) for f in frames if f.kind == "light"}
    for f in frames:
        if f.kind == "dark" and _family(f.path) not in light_fams:
            f.kind, f.status = "skip", "skipped"
            f.note = "dark is a different file type than the lights; not mixing pipelines"
            log(f"found {os.path.basename(f.path)}: a dark in a different format than the lights. "
                f"That came out of some other program. Not mixing pipelines. Ignored.")

    n_dark = sum(f.kind == "dark" for f in frames)
    log(f"{len(frames) - n_dark} lights, {n_dark} dark{'s' if n_dark != 1 else ''}. Fine.")

    # Unistellar drops a manifest.json next to the frames; scopepull keeps
    # the same thing inside observation.json. Say what this is.
    session = {}
    mf = read_manifest(args.folder if os.path.isdir(args.folder) else os.path.dirname(paths[0]))
    if mf:
        try:
            session = {"target": mf.get("nameTarget"), "sensor": mf.get("sensor"),
                       "exposure_s": (mf.get("expo") or 0) / 1e6, "gain": mf.get("gain"),
                       "saved": (mf.get("obs_attr") or {}).get("frames_saved"),
                       "scope_stacked": (mf.get("obs_attr") or {}).get("frames_stacked")}
            who = "scopepull pull" if mf.get("_source") == "observation.json" else "Unistellar session"
            log(f"{who}: {session['target']}  "
                f"{session['saved']} x {session['exposure_s']:.1f}s, gain {session['gain']}, "
                f"{session['sensor']}.  The scope itself gave up on "
                f"{(session['saved'] or 0) - (session['scope_stacked'] or 0)} of them. We'll see.")
        except Exception:
            session = {}

    # exposure per frame: the scope's manifest, else a FITS header
    exposure_s = float(session.get("exposure_s") or 0) if session else 0.0
    if not exposure_s:
        for f in frames:
            if f.kind == "light" and os.path.splitext(f.path)[1].lower() in FITS_EXT:
                try:
                    from astropy.io import fits as _fits
                    h = _fits.getheader(f.path)
                    exposure_s = float(h.get("EXPTIME") or h.get("EXPOSURE") or 0)
                except Exception:
                    exposure_s = 0.0
                break

    # ---- pass 1: shapes -----------------------------------------------------
    for f in frames:
        try:
            f.raw_shape, f.bayer, f.bits = probe(f.path)
        except Exception as e:
            f.status, f.note = "unreadable", str(e)[:80]
    lights = [f for f in frames if f.kind == "light" and f.status == "pending"]
    if not lights:
        sys.exit("couldn't read a single frame. Wrong folder, or not images.")

    # Things that live next to the subs but are not subs: the scope's own
    # finished stack, a stretched 8-bit preview, a thumbnail. They must not
    # be stacked and above all must not become the reference frame.
    NOT_A_SUB = ("preview", "thumb", "_thn", "stacksum", "stacked", "master", "final")
    RAW_EXT = FITS_EXT | {".tif", ".tiff"}
    have_raw = any(os.path.splitext(f.path)[1].lower() in RAW_EXT for f in lights)
    max_bits = max((f.bits for f in lights), default=16)
    groups = {}                                  # what -> [names], so 700 jpgs are one line, not 700
    for f in lights:
        name = os.path.basename(f.path).lower()
        ext = os.path.splitext(name)[1]
        why = what = None
        if any(w in name for w in NOT_A_SUB):
            why = "not a sub, judging by its name"
            what = ("a thumbnail" if "_thn" in name or "thumb" in name else
                    "a finished stack" if "stack" in name else
                    "a JPEG" if ext in (".jpg", ".jpeg") else "not a sub")
        elif have_raw and ext in (".jpg", ".jpeg"):
            why = "JPEG among raw frames (a preview, not data)"
            what = "a JPEG next to real frames"
        elif f.bits == 8 and max_bits >= 16:
            why = f"8-bit file among {max_bits}-bit subs (a preview, not data)"
            what = f"8-bit, in a pile of {max_bits}-bit subs"
        if why:
            f.kind, f.status, f.note = "skip", "skipped", why
            groups.setdefault(what, []).append(os.path.basename(f.path))
    plural = {"a thumbnail": "thumbnails", "a finished stack": "finished stacks", "a JPEG": "JPEGs",
              "a JPEG next to real frames": "JPEGs next to real frames", "not a sub": "not subs"}
    for what, names in groups.items():
        if len(names) <= 3:
            for n in names:
                log(f"found {n}. That's {what}. Removed it from the pile. You're welcome.")
        else:
            log(f"found {len(names)} files that are {plural.get(what, what)} ({names[0]} and friends). "
                f"Removed them from the pile. You're welcome.")
    lights = [f for f in lights if f.kind == "light"]
    darks = [f for f in frames if f.kind == "dark" and f.status == "pending"]
    if not lights:
        sys.exit("nothing left that looks like a sub. Wrong folder?")
    log(f"  {len(lights)} actual subs. Proceeding.")

    sizes = Counter(f.raw_shape[:2] for f in lights)
    major_shape = sizes.most_common(1)[0][0]
    if len(sizes) > 1:
        log("  frames come in more than one size. Some programs stop here. Not this one:")
        for shp, cnt in sizes.most_common():
            log(f"    {shp[1]}x{shp[0]}  {cnt:>5} frames")
        if args.no_align:
            log("  --no-align: odd sizes will be cropped/padded to the reference, top-left anchored")
        else:
            log("  everything gets mapped onto the reference grid. Nobody is left behind for being short.")

    usable = lights
    if args.max_frames:
        usable = usable[: args.max_frames]

    # ---- master dark --------------------------------------------------------
    master_dark = build_master_dark(darks, log) if darks else None
    if darks and master_dark is None:
        log("  darks were named like darks but couldn't be read. Stacking without. Hot pixels are your problem now.")

    # ---- bayer decision -----------------------------------------------------
    pattern = None
    if args.debayer.lower() not in ("none", "off", "no"):
        if args.debayer.lower() == "auto":
            hdr_pat = next((f.bayer for f in usable if f.bayer), None)
            is_mono = len(usable[0].raw_shape) == 2
            if hdr_pat and is_mono:
                pattern = hdr_pat
            elif is_mono:
                # no header (TIFF/PNG) -- look at the pixels
                try:
                    sample = read_image(usable[len(usable) // 2].path)
                    pattern = detect_bayer(sample)
                    if pattern:
                        log(f"no Bayer header, because TIFF. Looked at the pixels instead: "
                            f"it's a colour mosaic. Going with {pattern}. "
                            f"If the galaxy comes out blue, --debayer "
                            f"{'BGGR' if pattern == 'RGGB' else 'RGGB' if pattern == 'BGGR' else 'GRBG' if pattern == 'GBRG' else 'GBRG'}.")
                except Exception:
                    pattern = None
        else:
            pattern = args.debayer.upper()
    if pattern:
        log(f"debayering as {pattern}.")

    # ---- pick reference -----------------------------------------------------
    def load_prepared(f: Frame):
        _worker_init(master_dark, pattern, None, 0.0, 1.0, False, False)
        return _prepare(f.path)

    if args.ref is not None:
        ref_frame = usable[args.ref]
        ref_img = load_prepared(ref_frame)
    else:
        # only frames of the dominant size may be the reference: the output
        # grid is the reference grid, and an odd-sized straggler must not
        # decide the geometry for everyone else
        cands = [f for f in usable if f.raw_shape[:2] == major_shape] or usable
        probe_idx = np.linspace(0, len(cands) - 1, min(8, len(cands))).astype(int)
        best, best_n, best_img = None, -1, None
        for i in probe_idx:
            try:
                img = load_prepared(cands[i])
                n = star_count(luminance(img))
            except Exception:
                continue
            if n > best_n:
                best, best_n, best_img = cands[i], n, img
        if best is None:
            sys.exit("couldn't read any frame well enough to use as a reference. Giving up, reluctantly.")
        ref_frame, ref_img = best, best_img
        log(f"reference: {os.path.basename(ref_frame.path)} ({best_n} stars). Everyone else lines up to this one.")
        if best_n < args.min_stars and not args.no_align:
            log(f"  only {best_n} stars. Alignment may sulk. "
                f"--no-align if these frames are already registered.")

    ref_lum = luminance(ref_img)
    rl = ref_lum[np.isfinite(ref_lum)]
    ref_med = float(np.median(rl))
    ref_scale = float(np.median(np.abs(rl - ref_med))) + 1e-9
    out_shape = ref_img.shape

    # ---- pass 2: register into a memmapped cube -----------------------------
    align = not args.no_align
    if align:
        import astroalign as aa

    nbytes = len(usable) * int(np.prod(out_shape)) * 4
    streaming = args.method == "mean"
    scratch_root = args.scratch or tempfile.gettempdir()
    if not streaming:
        import shutil
        try:
            free = shutil.disk_usage(scratch_root).free
        except OSError:
            free = None
        need = int(nbytes * 1.05) + 512 * 1024 ** 2          # the cube, plus breathing room
        if free is not None and free < need:
            log(f"the scratch cube would be {nbytes/1e9:.1f} GB and {scratch_root} has "
                f"{free/1e9:.1f} GB free. Not going to fill your drive and die at 70%.")
            log(f"  falling back to a streaming mean: no scratch file, no outlier rejection, "
                f"no quality pass. Satellites may survive. "
                f"--scratch D:\\somewhere with room gets you the real thing.")
            streaming = True
            args.method = "mean"
    tmpdir = tempfile.mkdtemp(prefix="starstack_", dir=scratch_root)
    mm = None
    if not streaming:
        log(f"scratch cube: {nbytes/1e9:.2f} GB in {tmpdir}. It's temporary. Relax.")
        mm = np.lib.format.open_memmap(
            os.path.join(tmpdir, "cube.npy"), mode="w+",
            dtype=np.float32, shape=(len(usable),) + out_shape)
    acc = np.zeros(out_shape, np.float64)
    cnt = np.zeros(out_shape, np.uint16)

    kept = 0
    jobs = args.jobs or max(1, min(8, os.cpu_count() or 1))
    tasks = [(i, f.path, f is ref_frame) for i, f in enumerate(usable)]
    init_args = (master_dark, pattern, ref_lum, ref_med, ref_scale,
                 align, not args.no_normalize)

    def _consume(idx, status, note, img, metrics=None):
        nonlocal kept, cnt
        f = usable[idx]
        f.status, f.note = status, note
        if metrics:
            f.stars, f.fwhm, f.bg, f.noise = metrics["stars"], metrics["fwhm"], metrics["bg"], metrics["noise"]
        if img is None:
            return
        if streaming:
            m = np.isfinite(img)
            acc[m] += img[m]
            cnt += m
        else:
            mm[kept] = img
            f.slot = kept
        kept += 1

    done = 0
    if jobs > 1 and len(tasks) > 2:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=jobs, initializer=_worker_init,
                                 initargs=init_args) as ex:
            for idx, status, note, img, metrics in ex.map(_process_one, tasks, chunksize=2):
                _consume(idx, status, note, img, metrics)
                done += 1
                if not args.quiet and (done % 10 == 0 or done == len(tasks)):
                    print(f"\r  registering {done}/{len(tasks)}  kept {kept}", end="", flush=True)
    else:
        _worker_init(*init_args)
        for t in tasks:
            _consume(*_process_one(t))
            done += 1
            if not args.quiet and (done % 10 == 0 or done == len(tasks)):
                print(f"\r  registering {done}/{len(tasks)}  kept {kept}", end="", flush=True)
    if not args.quiet:
        print()

    if kept == 0:
        sys.exit("not one frame would align to the reference. Either these aren't the same sky, or there are no stars. Try --no-align if they're already registered.")

    # ---- quality pass -------------------------------------------------------
    registered = [f for f in usable if f.status == "used"]
    if streaming:
        log("  streaming mean: frames were summed as they came, so no quality pass. Everyone counts.")
    else:
        assess_quality(registered, log, keep_all=args.keep_all, use_weights=not args.no_weights)
    final = [f for f in registered if f.status == "used"]
    slots = [f.slot for f in final]
    weights = np.array([f.weight for f in final], np.float32)
    n_final = len(final) if not streaming else kept

    # ---- combine ------------------------------------------------------------
    log(f"combining {n_final} frames ({args.method}). This is the slow part"
        + (" -- every pixel gets a vote and the outliers get thrown out. Satellites, planes, cosmic rays: your time is coming."
           if args.method == "sigma" else "."))
    if streaming:
        with np.errstate(all="ignore"):
            stacked = (acc / np.maximum(cnt, 1)).astype(np.float32)
            stacked[cnt == 0] = np.nan
        coverage = cnt if cnt.ndim == 2 else cnt[..., 0]
    elif args.method == "median":
        cube = np.array(mm[slots])
        stacked = np.nanmedian(cube, axis=0).astype(np.float32)
        coverage = np.isfinite(cube).sum(axis=0)
        coverage = coverage if coverage.ndim == 2 else coverage[..., 0]
        del cube
    else:
        px = int(np.prod(out_shape[1:]))
        chunk_rows = max(1, min(out_shape[0], int(256e6 / max(len(slots) * px * 4, 1))))
        t_comb = time.time()

        def _prog(y, h):
            if args.quiet:
                return
            eta = ""
            if y > 0:
                left = (time.time() - t_comb) * (h - y) / y
                eta = f"  about {int(left // 60)}m{int(left % 60):02d}s left" if left >= 60 else f"  about {int(left)}s left"
            print(f"\r  combining rows {y}/{h}  ({100 * y // h}%){eta}      ", end="", flush=True)
        stacked, coverage = sigma_clip_stack(mm, slots, weights, args.sigma, args.iters, chunk_rows, _prog)
        if not args.quiet:
            print(f"\r  combining rows {out_shape[0]}/{out_shape[0]}  (100%)  outliers: gone.")
    kept = n_final

    stacked = np.nan_to_num(stacked, nan=0.0)

    # ---- write --------------------------------------------------------------
    log(f"writing {args.out}")
    ext = os.path.splitext(args.out)[1].lower()
    if args.bits == 16:
        # linear data on the 0..1 scale -> full 16-bit range; nothing is stretched
        out_arr = np.clip(stacked * 65535.0 + 0.5, 0, 65535).astype(np.uint16)
    else:
        out_arr = stacked.astype(np.float32)
    if ext in FITS_EXT:
        from astropy.io import fits as _fits
        data = np.moveaxis(out_arr, -1, 0) if out_arr.ndim == 3 else out_arr
        hdu = _fits.PrimaryHDU(data)
        hdu.header["STACKED"] = (kept, "frames combined by starstack")
        hdu.header["STACKMTH"] = args.method
        hdu.header["SWCREATE"] = f"starstack {__version__}"
        if exposure_s:
            hdu.header["EXPTIME"] = (exposure_s * kept, "total integration, seconds")
            hdu.header["EXPOSURE"] = (exposure_s, "per-frame exposure, seconds")
        if session.get("target"):
            hdu.header["OBJECT"] = session["target"]
        hdu.writeto(args.out, overwrite=True)
    else:
        import tifffile, json as _json
        meta = {"software": f"starstack {__version__}", "frames": kept, "method": args.method}
        if exposure_s:
            meta["exposure_s"] = exposure_s
            meta["integration_s"] = exposure_s * kept
        if session.get("target"):
            meta["target"] = session["target"]
        tifffile.imwrite(args.out, out_arr,
                         photometric="rgb" if out_arr.ndim == 3 else "minisblack",
                         description=_json.dumps(meta))
    if args.preview:
        from PIL import Image
        prev = (np.clip(autostretch(stacked), 0, 1) * 255).astype(np.uint8)
        Image.fromarray(prev).save(args.preview)

    if mm is not None:
        del mm
        try:
            os.remove(os.path.join(tmpdir, "cube.npy"))
            os.rmdir(tmpdir)
        except OSError:
            pass

    # ---- report -------------------------------------------------------------
    tally = Counter(f.status for f in frames)
    log("\n" + "-" * 46)
    log(f"  used          {tally['used']:>6}")
    for label, key in (("darks used", "dark"), ("unreadable", "unreadable"),
                       ("align failed", "align"), ("quality drop", "quality"), ("skipped", "skipped")):
        if tally[key]:
            log(f"  {label:<13} {tally[key]:>6}")
    log(f"  output        {args.out}  {stacked.shape}")
    if coverage is not None:
        log(f"  coverage      {int(coverage.min())}-{int(coverage.max())} frames/pixel")
    if exposure_s:
        total = exposure_s * kept
        mins, secs = divmod(int(round(total)), 60)
        hrs, mins = divmod(mins, 60)
        pretty = (f"{hrs}h {mins:02d}m {secs:02d}s" if hrs else f"{mins}m {secs:02d}s")
        log(f"  integration   {pretty}  ({kept} x {round(exposure_s, 2):g}s)")
    log(f"  elapsed       {time.time()-t0:.1f}s")
    log("-" * 46)

    if args.report:
        import csv
        with open(args.report, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["file", "kind", "status", "note", "raw_shape",
                        "stars", "fwhm_px", "background", "noise", "weight"])
            for f in frames:
                w.writerow([os.path.basename(f.path), f.kind, f.status, f.note, f.raw_shape,
                            f.stars, f"{f.fwhm:.2f}" if np.isfinite(f.fwhm) else "",
                            f"{f.bg:.5f}" if np.isfinite(f.bg) else "",
                            f"{f.noise:.5f}" if np.isfinite(f.noise) else "",
                            f"{f.weight:.2f}" if f.status == "used" else ""])
        log(f"per-frame report: {args.report}")

    rejected = tally["unreadable"] + tally["align"] + tally["quality"]
    if rejected:
        log(f"\n{rejected} frame(s) wouldn't cooperate and were left out. "
            f"{'--report tells you which. ' if not args.report else 'They are in the report. '}"
            f"That is normal. Not an error. Don't email me.")
    if log.fh:
        log(f"log: {_log_path_for(args.out)}")
    log("done. go outside.")
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
