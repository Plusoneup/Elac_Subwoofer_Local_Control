"""Generate the black ash wood textures used by the Black ash, Debut and Gunmetal themes.

Everything is procedural (seeded random noise, no photographs), so the files are original work
of this project. Both textures tile seamlessly: all noise is generated and blurred on a torus
(FFT with wrap-around), and the grain lines are positioned modulo the tile width.

    python tools/make_textures.py            # writes static/textures/black-ash*.jpg
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image

OUT = Path(__file__).resolve().parents[1] / "static" / "textures"


def blur(a, sx, sy):
    """Anisotropic Gaussian blur with wrap-around (keeps the texture tileable)."""
    h, w = a.shape
    fy = np.fft.fftfreq(h)[:, None]
    fx = np.fft.fftfreq(w)[None, :]
    k = np.exp(-2 * (math.pi ** 2) * ((fx * sx) ** 2 + (fy * sy) ** 2))
    return np.real(np.fft.ifft2(np.fft.fft2(a) * k))


def norm(a):
    a = a - a.min()
    return a / max(a.max(), 1e-9)


def wood(seed, w, h, n_lines, warp_px, groove_w, relief, fibre, mottle, base, gain, fleck_k=0.3, groove_k=0.55):
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    # Grain lines wander sideways: low-frequency warp, mostly varying along the length (y).
    warp = (norm(blur(rng.standard_normal((h, w)), w / 9, h / 7)) - 0.5) * 2 * warp_px
    warp += (norm(blur(rng.standard_normal((h, w)), w / 30, h / 40)) - 0.5) * warp_px * 0.35
    xs = x + warp
    # Irregularly spaced growth lines (positions modulo w), each with its own depth.
    pos = np.sort(rng.uniform(0, w, n_lines))
    depth = rng.uniform(0.35, 1.0, n_lines)
    width = groove_w * rng.uniform(0.6, 1.6, n_lines)
    height = np.zeros((h, w), np.float32)
    for p, d, gw in zip(pos, depth, width):
        dx = (xs - p + w / 2) % w - w / 2                    # wrap-around distance to the line
        # Lines fade in and out along their length, like real annual rings.
        fade = norm(blur(rng.standard_normal((h, 1)) * np.ones((1, 8)), 1, h / 12))[:, :1]
        height -= d * (0.35 + 0.65 * fade) * np.exp(-(dx / gw) ** 2)
    # Open-grain fibres: noise stretched hard along the grain.
    fib = norm(blur(rng.standard_normal((h, w)), 0.45, 9)) - 0.5
    fib2 = norm(blur(rng.standard_normal((h, w)), 1.2, 45)) - 0.5
    # Short bright flecks (light catching the fibre ends).
    fleck = np.clip(norm(blur(rng.standard_normal((h, w)), 0.5, 4)) - 0.62, 0, 1) * 3
    # Broad tonal patches (some boards are a little lighter).
    patch = norm(blur(rng.standard_normal((h, w)), w / 5, h / 6)) - 0.5
    # Relief shading of the grooves (light from the left) plus their own darkening.
    shade = np.roll(height, 1, axis=1) - np.roll(height, -1, axis=1)
    v = base + gain * (groove_k * height + relief * shade + fibre * (fib + 0.6 * fib2) + fleck_k * fleck + mottle * patch)
    v = np.clip(v, 0, 1)
    rgb = np.stack([v * 0.985, v * 0.99, v * 1.02], -1)      # very slightly cool black
    return Image.fromarray((np.clip(rgb, 0, 1) * 255).astype(np.uint8), "RGB")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    # Fine, dense grain with strong fibres (Debut, Gunmetal).
    wood(11, 640, 1152, n_lines=90, warp_px=12, groove_w=1.2, relief=0.15, fibre=1.3,
         mottle=0.3, base=0.1, gain=0.15, fleck_k=0.9).save(OUT / "black-ash.jpg", quality=82, optimize=True, progressive=True)
    # Wider, deeper grooves with visible relief (Black ash).
    wood(29, 864, 1728, n_lines=46, warp_px=34, groove_w=1.7, relief=1.1, fibre=0.75,
         mottle=0.12, base=0.17, gain=0.2, fleck_k=0.12, groove_k=0.85).save(OUT / "black-ash-2.jpg", quality=82, optimize=True, progressive=True)


if __name__ == "__main__":
    main()
