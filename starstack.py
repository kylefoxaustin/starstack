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
import os
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field

import numpy as np
import warnings

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
    status: str = "pending"             # used | unreadable | align | dark
    note: str = ""


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


def match_levels(img: np.ndarray, ref_med: float, ref_scale: float) -> np.ndarray:
    """Additive + multiplicative normalization so frames combine cleanly."""
    sample = img.reshape(-1)[::7]
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
    log(f"master dark: {len(imgs)} frames, median {float(np.median(master)):.5f}")
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
        return idx, "unreadable", str(e)[:80], None
    ref_lum = _W["ref_lum"]
    note = ""
    if not is_ref:
        if _W["align"]:
            try:
                import astroalign as aa
                tform, _ = aa.find_transform(luminance(img), ref_lum)
                img = warp_to_ref(tform, img, ref_lum.shape)
            except Exception as e:
                return idx, "align", str(e)[:80], None
        elif img.shape[:2] != ref_lum.shape:
            note = f"resized from {img.shape[1]}x{img.shape[0]}"
            img = crop_to(img, ref_lum.shape)
    if _W["normalize"]:
        img = match_levels(img, _W["ref_med"], _W["ref_scale"])
    return idx, "used", note, img.astype(np.float32)


# ----------------------------------------------------------------------------
# stacking
# ----------------------------------------------------------------------------

def sigma_clip_stack(mm: np.memmap, n: int, sigma: float, iters: int, chunk_rows: int):
    """NaN-aware sigma-clipped mean over a memmapped (N, H, W[, C]) cube."""
    shape = mm.shape[1:]
    out = np.zeros(shape, np.float32)
    cov = np.zeros(shape[:2], np.uint16)
    h = shape[0]
    for y0 in range(0, h, chunk_rows):
        y1 = min(y0 + chunk_rows, h)
        block = np.array(mm[:n, y0:y1], dtype=np.float32)
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
            cnt = keep.sum(axis=0)
            summed = np.where(keep, block, 0.0).sum(axis=0)
            out[y0:y1] = np.where(cnt > 0, summed / np.maximum(cnt, 1), np.nan)
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

def main(argv=None):
    p = argparse.ArgumentParser(
        prog="starstack",
        description="Point it at a folder of frames. Get one stacked image out.",
    )
    p.add_argument("folder", help="folder of frames (or a glob)")
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
    p.add_argument("--max-frames", type=int, default=None)
    p.add_argument("--min-stars", type=int, default=8,
                   help="frames with fewer detected stars are not used as reference")
    p.add_argument("--bits", type=int, choices=[16, 32], default=32,
                   help="output bit depth: 32 = float (default), 16 = integer")
    p.add_argument("--preview", default=None, help="also write a stretched PNG here")
    p.add_argument("--report", default=None, help="write a per-frame CSV report here")
    p.add_argument("-j", "--jobs", type=int, default=None,
                   help="parallel workers (default: CPU count, max 8)")
    p.add_argument("-q", "--quiet", action="store_true")
    args = p.parse_args(argv)

    log = (lambda *a: None) if args.quiet else (lambda *a: print(*a, flush=True))
    t0 = time.time()

    # ---- discover -----------------------------------------------------------
    import glob as _glob
    if any(ch in args.folder for ch in "*?["):
        paths = sorted(_glob.glob(args.folder))
    else:
        paths = sorted(
            os.path.join(args.folder, f)
            for f in os.listdir(args.folder)
            if os.path.splitext(f)[1].lower() in IMAGE_EXT
        )
    if not paths:
        sys.exit(f"no image files found in {args.folder}")
    frames = [Frame(path=p_) for p_ in paths]

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
            log(f"ignoring {os.path.basename(f.path)}: different file type than the lights")

    n_dark = sum(f.kind == "dark" for f in frames)
    log(f"found {len(frames) - n_dark} lights, {n_dark} darks")

    # Unistellar drops a manifest.json next to the frames; say what this is
    session = {}
    mpath = os.path.join(args.folder if os.path.isdir(args.folder) else
                         os.path.dirname(paths[0]), "manifest.json")
    if os.path.exists(mpath):
        try:
            import json
            with open(mpath) as fh:
                mf = json.load(fh)
            session = {"target": mf.get("nameTarget"), "sensor": mf.get("sensor"),
                       "exposure_s": (mf.get("expo") or 0) / 1e6, "gain": mf.get("gain"),
                       "saved": (mf.get("obs_attr") or {}).get("frames_saved"),
                       "scope_stacked": (mf.get("obs_attr") or {}).get("frames_stacked")}
            log(f"Unistellar session: {session['target']}  "
                f"{session['saved']} x {session['exposure_s']:.1f}s, gain {session['gain']}, "
                f"{session['sensor']}  (the scope itself kept {session['scope_stacked']})")
        except Exception:
            session = {}

    # ---- pass 1: shapes -----------------------------------------------------
    for f in frames:
        try:
            f.raw_shape, f.bayer, f.bits = probe(f.path)
        except Exception as e:
            f.status, f.note = "unreadable", str(e)[:80]
    lights = [f for f in frames if f.kind == "light" and f.status == "pending"]
    if not lights:
        sys.exit("no readable light frames")

    # Things that live next to the subs but are not subs: the scope's own
    # finished stack, a stretched 8-bit preview, a thumbnail. They must not
    # be stacked and above all must not become the reference frame.
    NOT_A_SUB = ("preview", "thumb", "stacksum", "stacked", "master", "final")
    major_bits = Counter(f.bits for f in lights).most_common(1)[0][0]
    for f in lights:
        name = os.path.basename(f.path).lower()
        why = None
        if any(w in name for w in NOT_A_SUB):
            why = "not a sub, judging by its name"
        elif f.bits == 8 and major_bits >= 16:
            why = f"8-bit file among {major_bits}-bit subs (a preview, not data)"
        if why:
            f.kind, f.status, f.note = "skip", "skipped", why
            log(f"skipping {os.path.basename(f.path)}: {why}")
    lights = [f for f in lights if f.kind == "light"]
    darks = [f for f in frames if f.kind == "dark" and f.status == "pending"]
    if not lights:
        sys.exit("no usable light frames")
    log(f"  {len(lights)} subs to stack")

    sizes = Counter(f.raw_shape[:2] for f in lights)
    major_shape = sizes.most_common(1)[0][0]
    if len(sizes) > 1:
        log("  frame sizes present (all will be used):")
        for shp, cnt in sizes.most_common():
            log(f"    {shp[1]}x{shp[0]}  {cnt:>5} frames")
        if args.no_align:
            log("  --no-align: odd sizes will be cropped/padded to the reference, top-left anchored")
        else:
            log("  star alignment will map every frame onto the reference frame's grid")

    usable = lights
    if args.max_frames:
        usable = usable[: args.max_frames]

    # ---- master dark --------------------------------------------------------
    master_dark = build_master_dark(darks, log) if darks else None
    if darks and master_dark is None:
        log("  no usable darks -- continuing without dark subtraction")

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
                        log(f"no Bayer header, but the pixels look like a colour mosaic: "
                            f"assuming {pattern} (Unistellar/Seestar). "
                            f"Override with --debayer if colours come out wrong.")
                except Exception:
                    pattern = None
        else:
            pattern = args.debayer.upper()
    if pattern:
        log(f"debayering as {pattern}")

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
            sys.exit("could not read any frame to use as a reference")
        ref_frame, ref_img = best, best_img
        log(f"reference: {os.path.basename(ref_frame.path)} ({best_n} stars detected)")
        if best_n < args.min_stars and not args.no_align:
            log(f"  warning: only {best_n} stars found -- alignment may fail. "
                f"Consider --no-align if these frames are already registered.")

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
    tmpdir = tempfile.mkdtemp(prefix="starstack_")
    mm = None
    if not streaming:
        log(f"scratch cube: {nbytes/1e9:.2f} GB in {tmpdir}")
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

    def _consume(idx, status, note, img):
        nonlocal kept, cnt
        f = usable[idx]
        f.status, f.note = status, note
        if img is None:
            return
        if streaming:
            m = np.isfinite(img)
            acc[m] += img[m]
            cnt += m
        else:
            mm[kept] = img
        kept += 1

    done = 0
    if jobs > 1 and len(tasks) > 2:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=jobs, initializer=_worker_init,
                                 initargs=init_args) as ex:
            for idx, status, note, img in ex.map(_process_one, tasks, chunksize=2):
                _consume(idx, status, note, img)
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
        sys.exit("no frames survived registration")

    # ---- combine ------------------------------------------------------------
    log(f"combining {kept} frames ({args.method})")
    if streaming:
        with np.errstate(all="ignore"):
            stacked = (acc / np.maximum(cnt, 1)).astype(np.float32)
            stacked[cnt == 0] = np.nan
        coverage = cnt if cnt.ndim == 2 else cnt[..., 0]
    elif args.method == "median":
        stacked = np.nanmedian(np.array(mm[:kept]), axis=0).astype(np.float32)
        coverage = np.isfinite(np.array(mm[:kept])).sum(axis=0)
        coverage = coverage if coverage.ndim == 2 else coverage[..., 0]
    else:
        px = int(np.prod(out_shape[1:]))
        chunk_rows = max(1, min(out_shape[0], int(256e6 / max(kept * px * 4, 1))))
        stacked, coverage = sigma_clip_stack(mm, kept, args.sigma, args.iters, chunk_rows)

    stacked = np.nan_to_num(stacked, nan=0.0)

    # ---- write --------------------------------------------------------------
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
        hdu.writeto(args.out, overwrite=True)
    else:
        import tifffile
        tifffile.imwrite(args.out, out_arr,
                         photometric="rgb" if out_arr.ndim == 3 else "minisblack")
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
                       ("align failed", "align"), ("skipped", "skipped")):
        if tally[key]:
            log(f"  {label:<13} {tally[key]:>6}")
    log(f"  output        {args.out}  {stacked.shape}")
    if coverage is not None:
        log(f"  coverage      {int(coverage.min())}-{int(coverage.max())} frames/pixel")
    log(f"  elapsed       {time.time()-t0:.1f}s")
    log("-" * 46)

    if args.report:
        import csv
        with open(args.report, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["file", "kind", "status", "note", "raw_shape"])
            for f in frames:
                w.writerow([os.path.basename(f.path), f.kind, f.status, f.note, f.raw_shape])
        log(f"per-frame report: {args.report}")

    rejected = tally["unreadable"] + tally["align"]
    if rejected and not args.quiet:
        print(f"\n{rejected} frame(s) were left out. "
              f"{'Run with --report to see which. ' if not args.report else ''}"
              f"That is normal and not an error.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
