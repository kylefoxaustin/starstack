"""Synthetic smart-scope-ish dataset: Bayer RGGB 16-bit FITS lights with dither,
rotation, hot pixels, a satellite streak, a few truncated frames, one corrupt
file, and named dark frames. Used to verify starstack end to end."""
import os, sys, numpy as np
from astropy.io import fits

rng = np.random.default_rng(7)
out = sys.argv[1] if len(sys.argv) > 1 else "testdata"
os.makedirs(out, exist_ok=True)
H, W = 1094, 1452
N_LIGHT = int(sys.argv[2]) if len(sys.argv) > 2 else 40     # smaller for CI
N_DARK = 8

# fixed pattern: hot pixels + bias
hot = np.zeros((H, W), np.float32)
hy, hx = rng.integers(0, H, 400), rng.integers(0, W, 400)
hot[hy, hx] = rng.uniform(0.15, 0.6, 400)
bias = 0.02
np.save(os.path.join(out, "_hot.npy"), np.stack([hy, hx]))

# star field in "sky" coords
NS = 250
sx = rng.uniform(-200, W + 200, NS); sy = rng.uniform(-200, H + 200, NS)
sb = rng.uniform(0.02, 0.9, NS) ** 2
np.save(os.path.join(out, "_stars.npy"), np.stack([sx, sy, sb]))

yy, xx = np.mgrid[0:H, 0:W]

def render(dx, dy, theta):
    c, s = np.cos(theta), np.sin(theta)
    cx, cy = W / 2, H / 2
    img = np.zeros((H, W), np.float32)
    for x, y, b in zip(sx, sy, sb):
        xr = c * (x - cx) - s * (y - cy) + cx + dx
        yr = s * (x - cx) + c * (y - cy) + cy + dy
        if -6 < xr < W + 6 and -6 < yr < H + 6:
            x0, x1 = int(max(0, xr - 6)), int(min(W, xr + 7))
            y0, y1 = int(max(0, yr - 6)), int(min(H, yr + 7))
            g = np.exp(-(((xx[y0:y1, x0:x1] - xr) ** 2 + (yy[y0:y1, x0:x1] - yr) ** 2) / (2 * 1.6 ** 2)))
            img[y0:y1, x0:x1] += b * g
    # faint nebula blob
    img += 0.03 * np.exp(-(((xx - 700 + dx) ** 2 + (yy - 500 + dy) ** 2) / (2 * 120 ** 2)))
    return img

def bayer(rgb_lum):
    # colour it: red-ish nebula, white stars -> simple channel gains through mask
    m = np.zeros((H, W), np.float32)
    m[0::2, 0::2] = 0.80; m[0::2, 1::2] = 1.0; m[1::2, 0::2] = 1.0; m[1::2, 1::2] = 0.70   # R G G B
    return rgb_lum * m

def to16(a):
    return np.clip(a * 65535, 0, 65535).astype(np.uint16)

def write(path, arr, extra=None):
    hdu = fits.PrimaryHDU(arr)
    hdu.header["BAYERPAT"] = "RGGB"
    hdu.header["EXPTIME"] = 10.0
    for k, v in (extra or {}).items():
        hdu.header[k] = v
    hdu.writeto(path, overwrite=True)

for i in range(N_LIGHT):
    dx, dy = rng.uniform(-25, 25), rng.uniform(-25, 25)
    th = np.deg2rad(rng.uniform(-1.5, 1.5))
    sky = bayer(render(dx, dy, th) * 0.5 + 0.06)   # mosaic modulates sky too, like a real OSC
    frame = sky + bias + hot + rng.normal(0, 0.012, (H, W)).astype(np.float32)
    if i == min(17, N_LIGHT - 1):   # satellite streak
        for t in np.linspace(0, 1, 3000):
            y, x = int(200 + t * 600), int(100 + t * 1200)
            if 0 <= y < H and 0 <= x < W:
                frame[y, x] += 0.7
    if i in (N_LIGHT - 4, N_LIGHT - 3, N_LIGHT - 2):   # truncated frames like Kyle's 1452x1088
        frame = frame[:1088]
    write(os.path.join(out, f"milkway test_{i:05d}.fit"), to16(frame))

# corrupt file
with open(os.path.join(out, f"milkway test_{N_LIGHT:05d}.fit"), "wb") as fh:
    fh.write(b"SIMPLE  =                    T / garbage" + b"\x00" * 500)

for i in range(N_DARK):
    d = bias + hot + rng.normal(0, 0.012, (H, W)).astype(np.float32)
    write(os.path.join(out, f"darkframe_{i:03d}.fit"), to16(d))

print("wrote", N_LIGHT + 1, "lights (1 corrupt, 3 truncated, 1 streaked) and", N_DARK, "darks to", out)
