# starstack

Dump a folder of frames in. Push one button. Get a stacked image out.

```
pip install -r requirements.txt
python starstack.py /path/to/frames -o result.tif --preview look.png
```

That's the whole workflow. No sequence files, no process folder, no
"register first, then stack", no state left on disk.

![M81 stacked with starstack from 710 Unistellar Odyssey Pro frames](docs/m81_example.png)

*M81 (Bode's Galaxy), 710 × 4 s from a Unistellar Odyssey Pro under Austin,
Texas skies. starstack was pointed at the session folder exactly as the
scope exported it -- dark applied, Bayer mosaic detected, 695 frames
aligned and sigma-clipped, the rest rejected. The linear stack was then
stretched and finished in AstroWizards.*

## Why

Stacking is one of those things that has to be hard for exactly one reason
(finding how each frame is shifted and rotated against the others) and is
made hard for several more that are just tooling: every program has its own
idea of what a "sub" looks like, guesses from metadata, and guesses wrong.
Siril wants a sequence file and dies with `input images have different sizes`
because the scope dropped its own finished stack into the same folder. Other
stackers see 32-bit float and decide the frames are already stacked. None of
that is the data's fault. This tool reads what is there and gets on with it.

## What it does without being asked

- **Reads whatever is in the folder** — TIFF, FITS, PNG, JPG, 8/16/32-bit,
  mono, RGB, or raw Bayer.
- **Finds your darks by name.** Anything with `darkframe`, `dark_`, `_dark`,
  etc. in the filename is a dark. They get median-combined into a master
  dark and subtracted from every light before anything else happens.
  `--darks some/folder` if they live elsewhere; `--no-darks` to ignore them.
- **Does not care about mixed frame sizes.** A camera that truncated the last
  few frames, or a mix of two ROIs, is not an error. Star alignment maps every
  frame onto the reference frame's grid regardless of its own size. (With
  `--no-align` they're cropped/padded top-left-anchored instead.)
- **Skips bad frames instead of dying.** Unreadable file, frame with no stars,
  alignment that won't converge — it's logged, left out, and the run
  continues. `--report frames.csv` tells you exactly which and why.
- **Ignores things that aren't subs.** A scope's own finished stack, a
  stretched 8-bit `preview.jpg`, a thumbnail — anything named like one
  (`preview`, `thumb`, `stacksum`, `stacked`, `master`, `final`) or that is
  8-bit when the real subs are 16-bit is left out. The reference frame is
  only ever picked from frames of the dominant size, so a straggler can
  never decide the output geometry.
- **Debayers raw frames, TIFFs included.** FITS get the pattern from the
  `BAYERPAT` header. TIFF/PNG have no header, so it looks at the pixels: a
  mosaic has four 2×2 phases with different means and a matching green pair
  on one diagonal, a mono or already-colour image doesn't. When it sees a
  mosaic it assumes RGGB (Unistellar, Seestar) and says so. Force with
  `--debayer RGGB`; `--debayer none` for frames that are already colour.
- **Picks its own reference frame** — the one with the most detectable stars
  out of a sample. Override with `--ref N`.
- **Star-aligns** with astroalign (translation + rotation + scale), so
  untracked and dithered data is fine.
- **Level-matches** every frame to the reference (median + spread) before
  combining, so a sky-brightness gradient across the session doesn't wreck
  the stack. `--no-normalize` to skip.
- **Sigma-clipped mean** by default — kills satellites, planes, cosmic rays.
  `--method mean` for a fast streaming average with no scratch file,
  `--method median` if you'd rather.

## Output

- `-o result.tif` — 32-bit float linear TIFF by default.
- `--bits 16` — 16-bit integer instead.
- `-o result.fit` — FITS instead of TIFF (either bit depth).
- `--preview look.png` — an auto-stretched PNG so you can see what you got
  without opening anything else.

The output is **linear and unstretched**, the way a stack should be. Stretch
it in whatever you like (Siril, GraXpert, Photoshop, GIMP).

## Options you may want

| flag | what |
|---|---|
| `--darks PATH` | folder or glob of darks (in addition to name-detected ones) |
| `--no-darks` | ignore darks entirely |
| `--debayer RGGB\|BGGR\|GRBG\|GBRG\|none\|auto` | Bayer pattern (default auto) |
| `--method sigma\|mean\|median` | combine method (default sigma) |
| `--sigma 3.0` | clip threshold in MADs |
| `--ref N` | force reference frame index |
| `--no-align` | frames are already registered |
| `--max-frames N` | stop after N lights (quick test runs) |
| `--bits 16\|32` | output bit depth |
| `-j N` | worker processes (default: CPU count, max 8) |
| `--report file.csv` | per-frame status |

## Disk and memory

Sigma clipping needs every registered frame at once, so they go into a
temporary memory-mapped scratch cube (`frames × height × width × channels × 4
bytes` — 500 colour frames at 1452×1094 is about 9.5 GB). It's created in
your temp directory and deleted when the run finishes. If that's too much,
`--method mean` streams and uses no scratch space.

## Smart scope notes

- **Unistellar Odyssey**: the portal only hands out TIFFs, and they are
  16-bit single-channel RGGB Bayer mosaics, LZW-compressed, 1452x1094
  (a 2x downsample of the IMX415). The pixel detector catches the mosaic
  and debayers as RGGB -- verified on a real M81 session: warm yellow
  core, white stars. A session folder also holds `DarkframeMean.tiff`
  (used as the master dark), `StackSum.tiff` (the scope's own finished
  stack -- 1452x1088, the file that makes Siril's `stack` fail with
  "different sizes"), `preview.jpg` and `manifest.json` (read for
  target/exposure/gain). Point the tool at the session folder as-is; it
  sorts all of that out.
- **Seestar**: sub-frames from the S50 are RGGB Bayer FITS with `BAYERPAT`
  set. Auto works.
- Files that came *out of Siril* are 32-bit float already-debayered or
  already-converted. That's why other stackers think they're finished
  products. This tool doesn't care — it reads them like anything else.

## Install

Python 3.10 or newer. Then, in a terminal (PowerShell is fine on Windows):

```
git clone https://github.com/kylefoxaustin/starstack
cd starstack
pip install -r requirements.txt
python starstack.py "C:\\path\\to\\your\\session folder" -o result.tif --bits 16 --preview look.png
```

`make_test_data.py` generates a synthetic 40-frame Bayer data set (with
darks, hot pixels, a satellite, truncated frames and a corrupt file) if you
want something to run against before pointing it at a real night.

## Verified against

The real thing: a 710-frame Unistellar Odyssey Pro session of M81, run on
the folder untouched -- `StackInput.tiff` lights, `DarkframeMean.tiff`,
`StackSum.tiff`, `preview.jpg`, `manifest.json` and a stray Siril
`master_dark.fit` all present. 695 frames aligned; the scope's own
stacker had kept 412. Result above.

Also a synthetic set, so the numbers can be checked:

40 dithered, rotated Bayer frames with 400 hot pixels, a satellite streak,
three truncated frames (1452×1088 among 1452×1094), one corrupt file and 8
named darks:

- hot pixels: 0.117 above background in a single sub → 0.0001 in the stack
- background noise: 6.8× lower than a single sub (√39 = 6.2)
- stars: tighter in the stack than in a single sub (no doubling)
- satellite streak: ~5× fainter under sigma clip vs plain mean
- 39 lights used, 8 darks used, 1 unreadable skipped, 3 odd-sized included
- the same frames re-exported as headerless 16-bit TIFFs (what the Odyssey
  portal gives you): mosaic detected from pixels, debayered RGGB, stacked
  to colour with darks applied

## License

MIT. Built by Kyle Fox with Claude, one very annoying evening with Siril.
