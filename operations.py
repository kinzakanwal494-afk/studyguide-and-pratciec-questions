"""Image operations using Pillow — filters, adjustments, transforms."""
from __future__ import annotations

import random
from typing import Optional, Tuple

from PIL import Image, ImageEnhance, ImageFilter, ImageOps


def ensure_rgba(im: Image.Image) -> Image.Image:
    return im.convert("RGBA")


def auto_tone(im: Image.Image) -> Image.Image:
    im = ensure_rgba(im)
    r, g, b, a = im.split()
    rgb = Image.merge("RGB", (r, g, b))
    rgb = ImageOps.autocontrast(rgb, cutoff=1)
    r, g, b = rgb.split()
    return Image.merge("RGBA", (r, g, b, a))


def auto_contrast(im: Image.Image) -> Image.Image:
    im = ensure_rgba(im)
    r, g, b, a = im.split()
    rgb = Image.merge("RGB", (r, g, b))
    rgb = ImageOps.autocontrast(rgb)
    r, g, b = rgb.split()
    return Image.merge("RGBA", (r, g, b, a))


def auto_color(im: Image.Image) -> Image.Image:
    im = ensure_rgba(im)
    r, g, b, a = im.split()
    rgb = Image.merge("RGB", (r, g, b))
    rgb = ImageOps.equalize(rgb.convert("L")).convert("RGB")
    rgb = Image.blend(Image.merge("RGB", (r, g, b)), rgb, 0.35)
    r, g, b = rgb.split()
    return Image.merge("RGBA", (r, g, b, a))


def grayscale_mode(im: Image.Image) -> Image.Image:
    im = ensure_rgba(im)
    gray = ImageOps.grayscale(im)
    return Image.merge("RGBA", (gray, gray, gray, im.split()[3]))


def rgb_mode(im: Image.Image) -> Image.Image:
    return ensure_rgba(im)


def adjust_brightness(im: Image.Image, factor: float) -> Image.Image:
    return ensure_rgba(ImageEnhance.Brightness(im).enhance(factor))


def adjust_contrast(im: Image.Image, factor: float) -> Image.Image:
    return ensure_rgba(ImageEnhance.Contrast(im).enhance(factor))


def adjust_saturation(im: Image.Image, factor: float) -> Image.Image:
    return ensure_rgba(ImageEnhance.Color(im).enhance(factor))


def gaussian_blur(im: Image.Image, radius: float = 2) -> Image.Image:
    return ensure_rgba(im.filter(ImageFilter.GaussianBlur(radius=radius)))


def box_blur(im: Image.Image, radius: int = 2) -> Image.Image:
    return ensure_rgba(im.filter(ImageFilter.BoxBlur(radius)))


def motion_blur(im: Image.Image, size: int = 10) -> Image.Image:
    im = ensure_rgba(im)
    size = max(3, size | 1)
    k = [0] * (size * size)
    mid = size // 2
    for x in range(size):
        k[mid * size + x] = 1
    return im.filter(ImageFilter.Kernel((size, size), k, size))


def sharpen(im: Image.Image) -> Image.Image:
    return ensure_rgba(im.filter(ImageFilter.SHARPEN))


def unsharp_mask(im: Image.Image) -> Image.Image:
    return ensure_rgba(im.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3)))


def edge_enhance(im: Image.Image) -> Image.Image:
    return ensure_rgba(im.filter(ImageFilter.EDGE_ENHANCE_MORE))


def add_noise(im: Image.Image, amount: float = 0.05) -> Image.Image:
    im = ensure_rgba(im)
    pixels = im.load()
    w, h = im.size
    amt = max(0.0, min(0.5, amount))
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            if random.random() > 0.3:
                continue
            r, g, b, a = pixels[x, y]
            n = int(255 * amt * (random.random() - 0.5))
            pixels[x, y] = (max(0, min(255, r + n)), max(0, min(255, g + n)), max(0, min(255, b + n)), a)
    return im


def add_noise_pil_only(im: Image.Image, amount: int = 15) -> Image.Image:
    """Gaussian-ish noise without numpy."""
    im = ensure_rgba(im)
    noise = Image.effect_noise(im.size, amount).convert("RGBA")
    n_r, n_g, n_b, _ = noise.split()
    r, g, b, a = im.split()
    blend = lambda base, n: Image.blend(base, n, 0.15)
    return Image.merge("RGBA", (blend(r, n_r), blend(g, n_g), blend(b, n_b), a))


def pixelate(im: Image.Image, block: int = 8) -> Image.Image:
    im = ensure_rgba(im)
    w, h = im.size
    block = max(2, block)
    small = im.resize((max(1, w // block), max(1, h // block)), Image.Resampling.NEAREST)
    return small.resize((w, h), Image.Resampling.NEAREST)


def emboss(im: Image.Image) -> Image.Image:
    return ensure_rgba(im.filter(ImageFilter.EMBOSS))


def find_edges(im: Image.Image) -> Image.Image:
    return ensure_rgba(im.filter(ImageFilter.FIND_EDGES))


def solarize(im: Image.Image, threshold: int = 128) -> Image.Image:
    return ensure_rgba(ImageOps.solarize(im.convert("RGB"), threshold).convert("RGBA"))


def posterize(im: Image.Image, bits: int = 4) -> Image.Image:
    im = ensure_rgba(im)
    r, g, b, a = im.split()
    rgb = Image.merge("RGB", (r, g, b))
    rgb = ImageOps.posterize(rgb, bits)
    r, g, b = rgb.split()
    return Image.merge("RGBA", (r, g, b, a))


def simple_clouds(size: Tuple[int, int], seed: Optional[int] = None) -> Image.Image:
    if seed is not None:
        random.seed(seed)
    w, h = size
    scale = 64
    img = Image.new("RGB", (max(1, w // scale), max(1, h // scale)))
    pix = img.load()
    for y in range(img.height):
        for x in range(img.width):
            v = int(255 * random.random())
            pix[x, y] = (v, v, v)
    img = img.resize((w, h), Image.Resampling.BILINEAR)
    img = img.filter(ImageFilter.GaussianBlur(radius=min(w, h) / 80))
    return img.convert("RGBA")


def lens_distort(im: Image.Image, strength: float = 0.15) -> Image.Image:
    """Light barrel-like effect: scale up and crop center (no numpy)."""
    im = ensure_rgba(im)
    w, h = im.size
    f = 1.0 + max(-0.9, min(0.9, strength)) * 0.08
    big = im.resize((max(1, int(w * f)), max(1, int(h * f))), Image.Resampling.BILINEAR)
    left = (big.width - w) // 2
    top = (big.height - h) // 2
    return big.crop((left, top, left + w, top + h))