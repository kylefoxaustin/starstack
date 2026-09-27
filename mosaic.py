"""Mosaic stacking for starstack.

A Seestar in framing mode (and any scope told to cover more sky than one
frame) doesn't shoot one field: it walks. Every sub is a full frame at a
different pointing, panels overlap their neighbours by a fifth or so, and
the far panels don't overlap each other at all. The scope's own stacker
tiles them; so does this.

How: the FITS headers say where each frame pointed (RA/DEC) and how big a
pixel is (XPIXSZ/FOCALLEN), but not which way the camera was turned. So
the reference frame goes down first, its nearest neighbours are registered
to it star-for-star, and from those exact placements the sky->canvas
orientation is fitted. After that every frame is predicted onto the canvas
from its header, registered against the mosaic-so-far at the predicted
spot (only that spot: astroalign works on the brightest stars, and the
brightest stars of an 8000-px canvas are mostly not in the overlap), and
placed where the stars say. Field rotation on an alt-az mount is handled
per frame by the registration itself.

Combining: no scratch cube -- a 3-hour session on a 65-megapixel canvas
would be hundreds of gigabytes. Instead the frames are read in passes:
pass 1 places them and gathers per-pixel mean and spread, pass 2 (and 3)
re-warp them with the stored transforms and keep only the pixels within
sigma of the mean, weighted like the normal stacker. Memory is a few
canvas-sized accumulators, disk-backed.
"""
from __future__ import annotations

import math
import os
import sys
import time

import numpy as np


def _ss():
    """starstack's live module. Run as a script it is __main__, and a second
    `import starstack` would be a different copy with empty worker state."""
    m = sys.modules.get("__main__")
    if m is not None and hasattr(m, "_prepare") and hasattr(m, "match_levels"):
        return m
    import starstack
    return starstack

# tunables
CONTROL_POINTS = 300      # astroalign control points: more than the default 50 so a 20 % overlap has a chance
PRED_MARGIN = 0.30        # crop margin around the predicted footprint, as a fraction of frame size
MIN_FIT = 3               # exact placements needed before the sky->canvas fit is trusted
FIT_SPREAD = 0.25         # ...and they must span at least this fraction of a frame in two directions


# ----------------------------------------------------------------------------
# headers: where did each frame point?
# ----------------------------------------------------------------------------

def read_pointing(path):
    """(ra_deg, dec_deg, arcsec_per_px, dateobs) from a FITS header, or None."""
    try:
        from astropy.io import fits
        h = fits.getheader(path)
        ra, dec = h.get("RA"), h.get("DEC")
        if ra is None or dec is None:
            ra, dec = h.get("CRVAL1"), h.get("CRVAL2")
        if ra is None or dec is None:
            return None
        ra, dec = float(ra), float(dec)
        if isinstance(h.get("RA"), str) or not np.isfinite(ra) or not np.isfinite(dec):
            return None
        scale = None
        px = h.get("XPIXSZ") or h.get("PIXSIZE1")
        fl = h.get("FOCALLEN")
        if px and fl:
            xb = h.get("XBINNING") or 1
            scale = 206.265 * float(px) * float(xb) / float(fl)
        return ra, dec, scale, h.get("DATE-OBS")
    except Exception:
        return None


def sky_offsets(pointings):
    """Tangent-plane offsets in arcsec from the centroid: x east-ish (RA), y north (Dec)."""
    ra = np.array([p[0] for p in pointings]); dec = np.array([p[1] for p in pointings])
    ra0, dec0 = float(np.median(ra)), float(np.median(dec))
    dra = (ra - ra0 + 180) % 360 - 180
    x = dra * 3600 * math.cos(math.radians(dec0))
    y = (dec - dec0) * 3600
    return np.stack([x, y], axis=1)


def looks_like_mosaic(name, offsets, scale, frame_hw):
    """Named like one, or the pointings wander more than a third of a frame."""
    if name and "mosaic" in os.path.basename(name).lower():
        return True
    if offsets is None or scale is None or len(offsets) < 3:
        return False
    span = np.ptp(offsets, axis=0) / scale            # px
    return bool(span[0] > 0.33 * frame_hw[1] or span[1] > 0.33 * frame_hw[0])


# ----------------------------------------------------------------------------
# the sky -> canvas model
# ----------------------------------------------------------------------------

class SkyModel:
    """Affine map from sky offsets (arcsec) to canvas coordinates of a frame's
    centre, fitted by least squares from frames that were placed by stars.
    Before enough placements exist, scale-only with a guessed orientation."""

    def __init__(self, scale, frame_hw, guess=None):
        self.scale = scale                       # arcsec / px
        self.frame_hw = frame_hw
        self.pairs = []                          # (sky xy, canvas xy)
        self.A = guess if guess is not None else np.array([[-1.0, 0.0], [0.0, 1.0]]) / scale
        self.t = np.zeros(2)
        self.fitted = False

    def add(self, sky, canvas_xy):
        self.pairs.append((np.asarray(sky, float), np.asarray(canvas_xy, float)))
        self._fit()

    def _fit(self):
        if len(self.pairs) < MIN_FIT:
            return
        S = np.array([p[0] for p in self.pairs]); C = np.array([p[1] for p in self.pairs])
        spread = np.ptp(S, axis=0) / self.scale
        if not self.fitted and (spread < FIT_SPREAD * min(self.frame_hw)).all():   # dithers only: too bunched to fit
            # still useful as a pure translation anchor
            self.t = np.mean(C - S @ self.A.T, axis=0)
            return
        X = np.hstack([S, np.ones((len(S), 1))])
        sol, *_ = np.linalg.lstsq(X, C, rcond=None)
        A = sol[:2].T
        # the fit must be a rotation/flip at the header scale, near enough; a wild
        # solution from three bunched points would send the next prediction anywhere
        det = abs(np.linalg.det(A)) * self.scale ** 2
        if 0.7 < det < 1.4:
            self.A, self.t = A, sol[2]
            self.fitted = True
        else:
            self.t = np.mean(C - S @ self.A.T, axis=0)

    def anchor(self, sky, canvas_xy):
        """First frame: define the translation so this sky point lands here."""
        self.t = np.asarray(canvas_xy, float) - self.A @ np.asarray(sky, float)
        self.pairs.append((np.asarray(sky, float), np.asarray(canvas_xy, float)))

    def predict(self, sky):
        return self.A @ np.asarray(sky, float) + self.t

    def rotation(self):
        """Angle (deg) and flip implied by A, for predicting a frame's footprint."""
        A = self.A * self.scale
        flip = np.linalg.det(A) < 0
        if flip:
            A = A @ np.diag([-1.0, 1.0])
        return math.degrees(math.atan2(A[1, 0], A[0, 0])), flip


def predicted_tform(model, sky, frame_hw):
    """A 3x3 frame->canvas transform from the model: the frame's centre at the
    predicted spot, rotated/flipped like the model says."""
    h, w = frame_hw
    ang, flip = model.rotation()
    a = math.radians(ang)
    R = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
    if flip:
        R = R @ np.diag([-1.0, 1.0])
    c = model.predict(sky)
    T = np.eye(3)
    T[:2, :2] = R
    T[:2, 2] = c - R @ np.array([w / 2, h / 2])
    return T


def footprint(T, frame_hw):
    """Canvas bbox (x0, y0, x1, y1) of a frame under T."""
    h, w = frame_hw
    corners = np.array([[0, 0, 1], [w, 0, 1], [0, h, 1], [w, h, 1]], float).T
    p = (T @ corners)[:2]
    return p[0].min(), p[1].min(), p[0].max(), p[1].max()


# ----------------------------------------------------------------------------
# registration against the mosaic-so-far
# ----------------------------------------------------------------------------

def _canvas_crop(lsum, cnt, box, canvas_hw):
    x0, y0, x1, y1 = box
    H, W = canvas_hw
    x0, y0 = max(0, int(math.floor(x0))), max(0, int(math.floor(y0)))
    x1, y1 = min(W, int(math.ceil(x1))), min(H, int(math.ceil(y1)))
    if x1 - x0 < 64 or y1 - y0 < 64:
        return None, (x0, y0)
    c = cnt[y0:y1, x0:x1]
    if (c > 0).sum() < 0.05 * c.size:
        return None, (x0, y0)
    with np.errstate(all="ignore"):
        crop = lsum[y0:y1, x0:x1] / np.maximum(c, 1)
    filled = crop[c > 0]
    crop = np.where(c > 0, crop, float(np.median(filled)))
    return crop.astype(np.float32), (x0, y0)


def register(lum, lsum, cnt, T_pred, frame_hw, canvas_hw, margin=PRED_MARGIN):
    """Find the exact frame->canvas transform by matching stars against the
    mosaic-so-far around the predicted footprint. Returns 3x3 or None."""
    import astroalign as aa
    x0, y0, x1, y1 = footprint(T_pred, frame_hw)
    mx, my = margin * frame_hw[1], margin * frame_hw[0]
    crop, (cx0, cy0) = _canvas_crop(lsum, cnt, (x0 - mx, y0 - my, x1 + mx, y1 + my), canvas_hw)
    if crop is None:
        return None
    try:
        tf, (s, t) = aa.find_transform(lum, crop, max_control_points=CONTROL_POINTS)
    except Exception:
        return None
    if len(s) < 8:
        return None
    T = np.asarray(tf.params, float).copy()
    T[0, 2] += cx0
    T[1, 2] += cy0
    # sanity: scale must be ~1 (same camera), and the placement near the prediction
    sc = math.sqrt(abs(np.linalg.det(T[:2, :2])))
    if not (0.9 < sc < 1.1):
        return None
    return T


def register_to_frame(lum, ref_lum):
    """Bootstrap: frame -> reference frame, full frames, no prediction."""
    import astroalign as aa
    try:
        tf, (s, t) = aa.find_transform(lum, ref_lum, max_control_points=CONTROL_POINTS)
    except Exception:
        return None
    if len(s) < 8:
        return None
    T = np.asarray(tf.params, float).copy()
    sc = math.sqrt(abs(np.linalg.det(T[:2, :2])))
    return T if 0.9 < sc < 1.1 else None


# ----------------------------------------------------------------------------
# warping a frame into its own bbox on the canvas
# ----------------------------------------------------------------------------

def warp_into(T, img, canvas_hw):
    """Warp img (H,W[,C]) by frame->canvas T. Returns (y0, y1, x0, x1, out)
    with out covering only the frame's bbox, NaN where the frame isn't."""
    import cv2
    H, W = canvas_hw
    fx0, fy0, fx1, fy1 = footprint(T, img.shape[:2])
    x0, y0 = max(0, int(math.floor(fx0))), max(0, int(math.floor(fy0)))
    x1, y1 = min(W, int(math.ceil(fx1)) + 1), min(H, int(math.ceil(fy1)) + 1)
    if x1 <= x0 or y1 <= y0:
        return None
    M = T[:2].copy()
    M[0, 2] -= x0
    M[1, 2] -= y0
    src = np.nan_to_num(img.astype(np.float32))
    out = cv2.warpAffine(src, M, (x1 - x0, y1 - y0), flags=cv2.INTER_LANCZOS4,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=0.0)
    ones = np.ones(img.shape[:2], np.float32)
    fp = cv2.warpAffine(ones, M, (x1 - x0, y1 - y0), flags=cv2.INTER_NEAREST,
                        borderMode=cv2.BORDER_CONSTANT, borderValue=0.0)
    # Lanczos reaches 3 px past the edge into the zero padding, which draws a
    # faint dark line along every frame border in a mosaic. Trim the rim.
    fp = cv2.erode(fp, np.ones((7, 7), np.uint8))
    out[fp < 0.5] = np.nan
    return y0, y1, x0, x1, out


# ----------------------------------------------------------------------------
# the stack
# ----------------------------------------------------------------------------

_MW = {}


def _mosaic_worker_init(master_dark, pattern, ref_med, ref_scale, normalize, canvas_hw):
    ss = _ss()
    ss._worker_init(master_dark, pattern, None, ref_med, ref_scale, False, normalize)
    _MW["canvas_hw"] = canvas_hw


def _mosaic_warp_one(job):
    """Pass 2+: re-read, re-level with the numbers pass 1 settled on, re-warp
    with the stored transform. Returns (idx, placed)."""
    idx, path, T, gain, add = job
    ss = _ss()
    try:
        img = ss._prepare(path).astype(np.float32)
        img = img * np.float32(gain) + np.asarray(add, np.float32)
        placed = warp_into(np.asarray(T), img, _MW["canvas_hw"])
        return idx, placed
    except Exception:
        return idx, None


def mosaic_stack(usable, ref_frame, ref_img, master_dark, pattern, args, log, session_name=None):
    """Returns (stacked, coverage, n_final). Frames get status/metrics/weight
    set like the normal path so the report and quality pass work unchanged."""
    ss = _ss()
    t0 = time.time()
    frame_hw = ref_img.shape[:2]
    H0, W0 = frame_hw

    # -- pointings ------------------------------------------------------------
    pts = [read_pointing(f.path) for f in usable]
    good = [i for i, p in enumerate(pts) if p is not None]
    if len(good) < len(usable):
        for i, f in enumerate(usable):
            if pts[i] is None:
                f.status, f.note = "align", "no RA/DEC in header; mosaic mode needs pointings"
        log(f"  {len(usable) - len(good)} frames have no RA/DEC and can't be placed. Left out.")
    usable = [usable[i] for i in good]
    pts = [pts[i] for i in good]
    if len(usable) < 2:
        raise SystemExit("mosaic mode needs at least two frames with RA/DEC in their headers.")
    scale = next((p[2] for p in pts if p[2]), None)
    if not scale:
        raise SystemExit("mosaic mode needs XPIXSZ and FOCALLEN in the headers to know the plate scale.")
    sky = sky_offsets(pts)
    span_px = np.ptp(sky, axis=0) / scale
    log(f"mosaic: {len(usable)} frames wander {span_px[0]:.0f} x {span_px[1]:.0f} px across the sky "
        f"({span_px[0]*scale/3600:.2f} x {span_px[1]*scale/3600:.2f} deg at {scale:.2f}\"/px). "
        f"One frame is {W0} x {H0}. Tiling them.")

    ref_i = usable.index(ref_frame)
    ref_lum = ss.luminance(ref_img)
    rl = ref_lum[np.isfinite(ref_lum)]
    ref_med = float(np.median(rl)); ref_scale = float(np.median(np.abs(rl - ref_med))) + 1e-9
    normalize = not args.no_normalize
    ch = ref_img.shape[2] if ref_img.ndim == 3 else 1
    ref_chan_med = [float(np.nanmedian(ref_img[..., c_] if ch > 1 else ref_img)) for c_ in range(ch)]

    # -- canvas: generous, cropped to coverage at the end --------------------
    diag = math.hypot(H0, W0)
    r = np.linalg.norm(sky - sky[ref_i], axis=1).max() / scale
    side = int(2 * r + diag * 1.15) + 64
    canvas_hw = (side, side)
    npx = side * side
    acc_bytes = npx * ch * 4
    log(f"  canvas {side} x {side} px while placing; the empty margin is cut off at the end. "
        f"Accumulators live on disk in {args.scratch or 'the temp folder'}: "
        f"{(acc_bytes * 4 + npx * 6) / 1e9:.1f} GB, no scratch cube.")

    import tempfile
    scratch_root = args.scratch or tempfile.gettempdir()
    tmpdir = tempfile.mkdtemp(prefix="starstack_mosaic_", dir=scratch_root)

    def mm(name, shape, dtype):
        return np.lib.format.open_memmap(os.path.join(tmpdir, name), mode="w+", dtype=dtype, shape=shape)

    cshape = canvas_hw + ((ch,) if ch > 1 else ())
    s1 = mm("sum.npy", cshape, np.float32)
    s2 = mm("sumsq.npy", cshape, np.float32)
    cnt = np.zeros(canvas_hw, np.uint16)
    lsum = np.zeros(canvas_hw, np.float32)        # registration target: mean luminance so far

    model = SkyModel(scale, frame_hw)
    centre = np.array([side / 2, side / 2])
    model.anchor(sky[ref_i], centre)

    def place(f, T, img):
        """Level-match, warp, accumulate. Returns the frame's canvas centre.

        Levels, two steps. First the frame's spread is scaled to the reference's
        (transparency, gain) and its median moved there -- the usual thing. Then,
        where the frame overlaps what's already on the canvas, the per-channel
        median difference to the mosaic-so-far is taken out. That second step is
        what a mosaic needs: a panel full of nebula has a higher median than the
        reference for a reason, and only the overlap shows the true level
        difference. Both steps are remembered per frame so later passes reproduce
        them exactly."""
        img = img.astype(np.float32)
        gain, add = 1.0, np.zeros(ch, np.float32)
        # One camera, one gain: a frame's own spread says nothing about its gain
        # in a mosaic (a panel of nebula spreads more than a panel of sky), so
        # the gain stays 1 and only levels move. Per channel, so colour survives.
        if normalize:
            for c_ in range(ch):
                chan = (img[..., c_] if ch > 1 else img).reshape(-1)[::7]
                chan = chan[np.isfinite(chan)]
                add[c_] = ref_chan_med[c_] - float(np.median(chan))
            img = img + add
        placed = warp_into(T, img, canvas_hw)
        if placed is None:
            return None
        y0, y1, x0, x1, out = placed
        m = np.isfinite(out if out.ndim == 2 else out[..., 0])
        if normalize:
            c = cnt[y0:y1, x0:x1]
            ov = m & (c > 0)
            if ov.sum() >= 2000:
                with np.errstate(all="ignore"):
                    so_far = s1[y0:y1, x0:x1] / (c[..., None] if out.ndim == 3 else c)
                a = out[ov].reshape(int(ov.sum()), -1)              # this frame, overlap pixels
                b = so_far[ov].reshape(int(ov.sum()), -1)           # the mosaic there
                d = np.median(b - a, axis=0).astype(np.float32)
                out = out + d
                add = add + d
        f.level = (float(gain), add.tolist())
        o = np.nan_to_num(out)
        s1[y0:y1, x0:x1] += o
        s2[y0:y1, x0:x1] += o * o
        cnt[y0:y1, x0:x1] += m
        lsum[y0:y1, x0:x1] += np.where(m, ss.luminance(o) if o.ndim == 3 else o, 0.0)
        f.tform = T.tolist()
        c = T @ np.array([W0 / 2, H0 / 2, 1.0])
        return c[:2]

    # -- pass 1: place, nearest the reference first ---------------------------
    order = sorted(range(len(usable)), key=lambda i: np.linalg.norm(sky[i] - sky[ref_i]))
    T_ref = np.eye(3); T_ref[:2, 2] = centre - np.array([W0 / 2, H0 / 2])
    try:
        m_ = ss.frame_metrics(ref_lum)
        ref_frame.stars, ref_frame.fwhm, ref_frame.bg, ref_frame.noise = m_["stars"], m_["fwhm"], m_["bg"], m_["noise"]
    except Exception:
        pass
    place(ref_frame, T_ref, ref_img)
    ref_frame.status = "used"
    placed_n, failed = 1, 0
    orientations = None
    for n, i in enumerate(order, 1):
        f = usable[i]
        if f is ref_frame:
            continue
        try:
            img = ss._prepare(f.path)
        except Exception as e:
            f.status, f.note = "unreadable", str(e)[:80]
            continue
        lum = ss.luminance(img)
        try:
            m_ = ss.frame_metrics(lum)
            f.stars, f.fwhm, f.bg, f.noise = m_["stars"], m_["fwhm"], m_["bg"], m_["noise"]
        except Exception:
            pass
        T = None
        if model.fitted:
            T = register(lum, lsum, cnt, predicted_tform(model, sky[i], frame_hw), frame_hw, canvas_hw)
            if T is None:                                  # once more, wider
                T = register(lum, lsum, cnt, predicted_tform(model, sky[i], frame_hw), frame_hw, canvas_hw,
                             margin=PRED_MARGIN * 2.5)
        if T is None:
            # no trusted orientation yet (or the prediction missed): try the eight ways
            # the camera could be turned, cheapest first (the current guess)
            if orientations is None:
                orientations = []
                for flip in (False, True):
                    for ang in (0, 90, 180, 270):
                        a = math.radians(ang)
                        R = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
                        if flip:
                            R = R @ np.diag([-1.0, 1.0])
                        orientations.append(R / scale)
            A_saved, t_saved = model.A.copy(), model.t.copy()
            tried = [model.A.copy()] + [A for A in orientations if not np.allclose(A, model.A)]
            for A in tried:
                model.A = A
                model.t = model.pairs[0][1] - A @ model.pairs[0][0]
                T = register(lum, lsum, cnt, predicted_tform(model, sky[i], frame_hw), frame_hw, canvas_hw,
                             margin=PRED_MARGIN * 1.5)
                if T is not None:
                    break
            if T is None:
                model.A, model.t = A_saved, t_saved
        if T is None:
            f.status, f.note = "align", "couldn't match it to the mosaic-so-far"
            failed += 1
        else:
            c = place(f, T, img)
            if c is None:
                f.status, f.note = "align", "landed off the canvas"
                failed += 1
            else:
                f.status = "used"
                placed_n += 1
                model.add(sky[i], c)
        if not args.quiet and (n % 5 == 0 or n == len(order)):
            ang, flip = model.rotation()
            print(f"\r  placing {n}/{len(order)}  placed {placed_n}, lost {failed}"
                  f"{'  (camera at %.1f deg%s)' % (ang, ', mirrored' if flip else '') if model.fitted else ''}      ",
                  end="", flush=True)
    if not args.quiet:
        print()
    if placed_n < 2:
        raise SystemExit("couldn't tile these: not one frame would match the reference. Are they the same sky?")
    ang, flip = model.rotation()
    log(f"  placed {placed_n} of {len(usable)}; camera turned {ang:.1f} deg{' and mirrored' if flip else ''} "
        f"relative to north-up. {failed} couldn't be matched." if failed else
        f"  placed all {placed_n}; camera turned {ang:.1f} deg{' and mirrored' if flip else ''} relative to north-up.")

    # -- quality pass (same rules as always) ----------------------------------
    registered = [f for f in usable if f.status == "used"]
    ss.assess_quality(registered, log, keep_all=args.keep_all, use_weights=not args.no_weights)
    final = [f for f in registered if f.status == "used"]
    # pixels of frames the quality pass dropped are in the pass-1 statistics; that
    # only makes the first estimate of mean/spread slightly worse. Pass 2 leaves them out.

    # -- crop the canvas to what got covered ----------------------------------
    ys, xs = np.nonzero(cnt)
    cy0, cy1, cx0, cx1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    log(f"  mosaic covers {cx1 - cx0} x {cy1 - cy0} px. Cutting the canvas to that.")

    # -- passes 2..n: sigma-clipped, weighted -----------------------------------
    jobs = args.jobs or max(1, min(8, os.cpu_count() or 1))
    init = (master_dark, pattern, ref_med, ref_scale, normalize, canvas_hw)
    n_clip = 0 if args.method == "mean" else max(1, min(int(args.iters), 2))
    if n_clip:
        import shutil
        try:
            free = shutil.disk_usage(scratch_root).free
        except OSError:
            free = None
        need = acc_bytes * 2 + npx * 4 + 256 * 1024 ** 2          # the clipped sums, on top of pass 1's
        if free is not None and free < need:
            log(f"  the clipping passes want {need/1e9:.1f} GB more in {scratch_root} and there's "
                f"{free/1e9:.1f} GB. Keeping the plain mean instead: no outlier rejection. "
                f"--scratch somewhere with room gets you the real thing.")
            n_clip = 0
    weights = {id(f): float(getattr(f, "weight", 1.0) or 1.0) for f in final}
    # statistics the next pass clips against: (weighted sum, weighted sum of squares,
    # weight total, how many frames). Pass 1's are unweighted; that's fine for a first cut.
    st_s1, st_s2, st_w, st_n = s1, s2, cnt.astype(np.float32), cnt
    floor = ref_scale * 1.4826 * 0.5                       # never clip on a spread smaller than the noise
    stacked = coverage = None
    for p in range(n_clip):
        log(f"  clipping pass {p + 1}/{n_clip}: every frame again, keeping pixels within "
            f"{args.sigma:g} sigma of the mosaic. "
            + ("This is the slow part." if p == 0 else "Once more, against the cleaned-up mosaic."))
        ns1 = mm(f"csum{p}.npy", cshape, np.float32)
        ns2 = mm(f"csumsq{p}.npy", cshape, np.float32) if p + 1 < n_clip else None
        nw = mm(f"cw{p}.npy", canvas_hw, np.float32)
        nn = np.zeros(canvas_hw, np.uint16)
        tasks = [(k, f.path, f.tform, f.level[0], f.level[1]) for k, f in enumerate(final)]
        done = 0
        t_p = time.time()

        def _take(k, placed):
            nonlocal done
            done += 1
            if placed is not None:
                y0, y1, x0, x1, out = placed
                w = weights[id(final[k])]
                colour = out.ndim == 3
                m = np.isfinite(out[..., 0] if colour else out)
                wt = st_w[y0:y1, x0:x1]
                wt3 = wt[..., None] if colour else wt
                with np.errstate(all="ignore"):
                    mean = st_s1[y0:y1, x0:x1] / np.maximum(wt3, 1e-6)
                    std = np.sqrt(np.maximum(st_s2[y0:y1, x0:x1] / np.maximum(wt3, 1e-6) - mean * mean, 0.0))
                std = np.maximum(std, floor)
                o = np.nan_to_num(out)
                keep = np.abs(o - mean) <= args.sigma * std
                if colour:
                    keep = keep.all(axis=-1)
                keep |= st_n[y0:y1, x0:x1] < 3                 # too few votes to call anything an outlier
                keep &= m
                o = np.where(keep[..., None] if colour else keep, o, 0.0)
                ns1[y0:y1, x0:x1] += o * w
                nw[y0:y1, x0:x1] += keep * w
                nn[y0:y1, x0:x1] += keep
                if ns2 is not None:
                    ns2[y0:y1, x0:x1] += o * o * w
            if not args.quiet and (done % 5 == 0 or done == len(tasks)):
                el = time.time() - t_p
                eta = el / done * (len(tasks) - done)
                print(f"\r    frames {done}/{len(tasks)}  about {int(eta // 60)}m{int(eta % 60):02d}s left      ", end="", flush=True)

        if jobs > 1 and len(tasks) > 2:
            from concurrent.futures import ProcessPoolExecutor
            with ProcessPoolExecutor(max_workers=jobs, initializer=_mosaic_worker_init, initargs=init) as ex:
                for k, placed in ex.map(_mosaic_warp_one, tasks, chunksize=1):
                    _take(k, placed)
        else:
            _mosaic_worker_init(*init)
            for t in tasks:
                _take(*_mosaic_warp_one(t))
        if not args.quiet:
            print()
        if ns2 is not None:
            st_s1, st_s2, st_w, st_n = ns1, ns2, nw, nn
        else:
            with np.errstate(all="ignore"):
                stacked = ns1 / np.maximum(nw[..., None] if ch > 1 else nw, 1e-6)
            stacked = np.where((nw > 0)[..., None] if ch > 1 else (nw > 0), stacked, np.nan).astype(np.float32)
            coverage = nn
    if n_clip == 0:
        with np.errstate(all="ignore"):
            stacked = s1 / np.maximum(cnt[..., None] if ch > 1 else cnt, 1)
        stacked = np.where((cnt > 0)[..., None] if ch > 1 else (cnt > 0), stacked, np.nan).astype(np.float32)
        coverage = cnt.copy()

    stacked = np.array(stacked[cy0:cy1, cx0:cx1])
    coverage = np.array(coverage[cy0:cy1, cx0:cx1])
    # tidy the disk
    for name in list(os.listdir(tmpdir)):
        try:
            os.remove(os.path.join(tmpdir, name))
        except OSError:
            pass
    try:
        del s1, s2
        os.rmdir(tmpdir)
    except OSError:
        pass
    log(f"  tiled in {time.time() - t0:.0f}s.")
    return stacked, coverage, len(final)
