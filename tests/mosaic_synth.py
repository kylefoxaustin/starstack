"""A synthetic Seestar-style mosaic session, for testing mosaic stacking.

One wide star field (with a soft nebula so seams would show), cut into
frames the way a Seestar in framing mode does it: several panels, each
visited more than once, dithered, drifting in rotation as the night goes
on (alt-az field rotation), each frame a GRBG Bayer mosaic with RA/DEC,
XPIXSZ, FOCALLEN and BAYERPAT in the header. The sky->pixel orientation
(angle + flip) is a parameter the stacker must NOT be told.
"""
import os
import numpy as np


def wide_field(rng, H, W, nstars=900):
    yy, xx = np.mgrid[0:H, 0:W]
    scene = np.full((H, W), 0.06, np.float32)
    # a nebula: two broad gaussians so panel seams would be visible if levels drift
    for cx, cy, sx, sy, amp in ((W * 0.45, H * 0.5, W * 0.18, H * 0.22, 0.10),
                                (W * 0.62, H * 0.38, W * 0.09, H * 0.12, 0.07)):
        scene += (amp * np.exp(-(((xx - cx) / sx) ** 2 + ((yy - cy) / sy) ** 2) / 2)).astype(np.float32)
    stars = np.stack([rng.uniform(0, W, nstars), rng.uniform(0, H, nstars),
                      rng.uniform(0.15, 0.9, nstars) ** 2], axis=1)
    for sx, sy, amp in stars:
        x0, y0 = int(sx), int(sy)
        ys, xs = slice(max(0, y0 - 7), y0 + 8), slice(max(0, x0 - 7), x0 + 8)
        scene[ys, xs] += amp * np.exp(-(((xx[ys, xs] - sx) ** 2 + (yy[ys, xs] - sy) ** 2) / (2 * 1.9 ** 2))).astype(np.float32)
    return scene, stars


def cut_frame(scene, cx, cy, theta_deg, fh, fw):
    """Sample a fh x fw frame from the scene centred at (cx, cy), rotated by theta."""
    import cv2
    M = cv2.getRotationMatrix2D((float(cx), float(cy)), float(theta_deg), 1.0)
    M[0, 2] += fw / 2 - cx
    M[1, 2] += fh / 2 - cy
    return cv2.warpAffine(scene, M, (fw, fh), flags=cv2.INTER_LINEAR, borderValue=0.0)


def bayer_grbg(mono, rng):
    gain = np.ones_like(mono)
    gain[0::2, 1::2] = 0.9      # R
    gain[1::2, 0::2] = 0.6      # B
    frame = mono * gain + rng.normal(0, 0.004, mono.shape).astype(np.float32)
    return np.clip(frame * 65535, 0, 65535).astype(np.uint16)


def make_session(d, rng=None, fh=800, fw=560, panels=((0, 0), (1, 0), (0, 1), (1, 1), (2, 0.5)),
                 visits=3, per_visit=2, overlap=0.22, sky_angle=37.0, flip=True,
                 scale_arcsec=2.3, rot_drift_deg=6.0, target="NGC 7000", scene_hw=None):
    """Write the session into folder d. Returns (scene, truth) where truth maps
    each filename to its scene-pixel centre and rotation."""
    rng = rng or np.random.default_rng(5)
    os.makedirs(d, exist_ok=True)
    from astropy.io import fits
    step_x, step_y = fw * (1 - overlap), fh * (1 - overlap)
    H, W = scene_hw or (int(fh * 2.6), int(fw * 3.4))
    scene, stars = wide_field(rng, H, W)
    ox, oy = W * 0.28, H * 0.28
    # sky <-> scene: scene = R(sky_angle) * F * sky / scale   (F flips x when flip)
    a = np.radians(sky_angle)
    F = np.diag([-1.0 if flip else 1.0, 1.0])
    R = np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]])
    A = R @ F / scale_arcsec                     # arcsec -> scene px
    Ainv = np.linalg.inv(A)
    ra0, dec0 = 314.95, 44.44
    truth = {}
    n = 0
    order = []
    for v in range(visits):
        for px, py in panels:
            for k in range(per_visit):
                order.append((v, px, py, k))
    t_total = len(order)
    for i, (v, px, py, k) in enumerate(order):
        cx = ox + px * step_x + rng.uniform(-18, 18)
        cy = oy + py * step_y + rng.uniform(-18, 18)
        theta = rot_drift_deg * i / max(t_total - 1, 1)     # field rotation over the night
        mono = cut_frame(scene, cx, cy, theta, fh, fw)
        frame = bayer_grbg(mono, rng)
        # header RA/DEC from the scene position through the (secret) orientation
        sky = Ainv @ np.array([cx - ox, cy - oy])            # arcsec offsets from panel (0,0)
        ra = ra0 + sky[0] / 3600 / np.cos(np.radians(dec0))
        dec = dec0 + sky[1] / 3600
        hh, mm = divmod(i, 60)
        name = f"Light_mosaic_{target}_10.0s_LP_20260926-{22 + hh:02d}{mm:02d}{(i * 7) % 60:02d}.fit"
        h = fits.PrimaryHDU(frame)
        h.header["BAYERPAT"] = "GRBG"; h.header["EXPTIME"] = 10.0; h.header["EXPOSURE"] = 10.0
        h.header["RA"] = float(ra); h.header["DEC"] = float(dec)
        h.header["XPIXSZ"] = 2.9; h.header["YPIXSZ"] = 2.9; h.header["FOCALLEN"] = 260.0 * (2.9 / scale_arcsec) * (2.3 / 2.9) * (scale_arcsec / 2.3)
        h.header["FOCALLEN"] = 206.265 * 2.9 / scale_arcsec          # so that scale comes out as scale_arcsec
        h.header["OBJECT"] = target; h.header["CREATOR"] = "synthetic Seestar"
        h.header["DATE-OBS"] = f"2026-09-27T{hh:02d}:{mm:02d}:{(i * 7) % 60:02d}"
        h.writeto(os.path.join(d, name), overwrite=True)
        truth[name] = (cx, cy, theta)
        n += 1
    return scene, truth


if __name__ == "__main__":
    import sys
    make_session(sys.argv[1] if len(sys.argv) > 1 else "synthetic_mosaic")
