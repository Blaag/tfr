from __future__ import annotations

import binascii
import io
import os
import re
import selectors
import shutil
import stat
import subprocess
import sys
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image, ImageGrab, ImageOps, UnidentifiedImageError

DEFAULT_IMAGE_WIDTH = 72
MAXIMUM_IMAGE_WIDTH = 80
MAXIMUM_IMAGE_HEIGHT = 40
MAXIMUM_IMAGE_FILE_BYTES = 20 * 1024 * 1024
MAXIMUM_IMAGE_PIXELS = 16_000_000
IMAGE_COLOR_LIMIT = 64
CLIPBOARD_TIMEOUT_SECONDS = 10

ImageGlyphMode = Literal["ascii", "braille"]

_ASCII_GLYPHS = " .:-=+*#%@"
_SUPPORTED_IMAGE_FORMATS = frozenset({"BMP", "GIF", "ICO", "JPEG", "PNG", "TIFF", "WEBP"})
_SAFE_IMAGE_LINE = re.compile(r"(?:[\x20-\x7e\u2800-\u28ff]|\x1b\[(?:0|38;5;\d{1,3})m)*")
_BRAILLE_DOTS = (
    ((0x01, 16), (0x08, 144)),
    ((0x02, 208), (0x10, 80)),
    ((0x04, 48), (0x20, 176)),
    ((0x40, 240), (0x80, 112)),
)


@dataclass(frozen=True, slots=True)
class RenderedImage:
    lines: tuple[str, ...]
    width: int
    height: int
    mode: ImageGlyphMode


def load_image_file(path: Path | str) -> Image.Image:
    source = Path(path).expanduser()
    try:
        descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as image_file:
            file_status = stat.S_ISREG(os.fstat(image_file.fileno()).st_mode)
            if not file_status:
                raise ValueError("image path must name a regular file")
            content = image_file.read(MAXIMUM_IMAGE_FILE_BYTES + 1)
        if len(content) > MAXIMUM_IMAGE_FILE_BYTES:
            raise ValueError(f"image file exceeds the {MAXIMUM_IMAGE_FILE_BYTES} byte limit")
        return _decode_image(io.BytesIO(content))
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
    ) as exc:
        raise ValueError("file is not a supported image") from exc


def load_clipboard_image() -> Image.Image:
    try:
        if sys.platform == "darwin":
            image = _macos_clipboard_image()
        elif sys.platform.startswith("linux"):
            image = _linux_clipboard_image()
        elif sys.platform == "win32":
            raise ValueError("clipboard images are not yet supported on Windows; use /image PATH")
        else:
            image = ImageGrab.grabclipboard()
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        ChildProcessError,
        OSError,
        NotImplementedError,
        ValueError,
    ) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError("clipboard images are unavailable on this host") from exc
    if not isinstance(image, Image.Image):
        raise ValueError("clipboard does not contain an image")
    _validate_source_dimensions(image)
    return ImageOps.exif_transpose(image).convert("RGBA")


def _macos_clipboard_image() -> Image.Image | None:
    returncode, output = _run_bounded(
        ["osascript", "-e", "get the clipboard as «class PNGf»"],
        maximum_bytes=MAXIMUM_IMAGE_FILE_BYTES * 2 + 32,
    )
    if returncode != 0:
        return None
    try:
        content = binascii.unhexlify(output[11:-3])
    except (binascii.Error, ValueError) as exc:
        raise ValueError("clipboard does not contain a readable PNG image") from exc
    return _decode_image(io.BytesIO(content))


def _linux_clipboard_image() -> Image.Image | None:
    if shutil.which("wl-paste") and (
        os.environ.get("WAYLAND_DISPLAY") or not os.environ.get("DISPLAY")
    ):
        command = ["wl-paste", "-t", "image/png"]
    elif shutil.which("xclip"):
        command = ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]
    else:
        raise ValueError("clipboard images require wl-paste or xclip on Linux")
    returncode, output = _run_bounded(command, maximum_bytes=MAXIMUM_IMAGE_FILE_BYTES)
    if returncode != 0:
        return None
    return _decode_image(io.BytesIO(output))


def _run_bounded(command: list[str], *, maximum_bytes: int) -> tuple[int, bytes]:
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert process.stdout is not None
    os.set_blocking(process.stdout.fileno(), False)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    output = bytearray()
    deadline = time.monotonic() + CLIPBOARD_TIMEOUT_SECONDS
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("clipboard image read timed out")
            if not selector.select(remaining):
                raise ValueError("clipboard image read timed out")
            chunk = os.read(process.stdout.fileno(), min(65_536, maximum_bytes + 1 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > maximum_bytes:
                raise ValueError(
                    f"clipboard image exceeds the {MAXIMUM_IMAGE_FILE_BYTES} byte limit"
                )
        try:
            return process.wait(timeout=max(0.01, deadline - time.monotonic())), bytes(output)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("clipboard image read timed out") from exc
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait()


def _decode_image(content: io.BytesIO) -> Image.Image:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(content) as image:
            if image.format not in _SUPPORTED_IMAGE_FORMATS:
                raise ValueError(f"image format {image.format or 'unknown'} is not supported")
            image.seek(0)
            _validate_source_dimensions(image)
            image.load()
            return ImageOps.exif_transpose(image).convert("RGBA")


def render_image(
    image: Image.Image,
    *,
    width: int = DEFAULT_IMAGE_WIDTH,
    mode: ImageGlyphMode = "ascii",
) -> RenderedImage:
    if not 1 <= width <= MAXIMUM_IMAGE_WIDTH:
        raise ValueError(f"image width must be between 1 and {MAXIMUM_IMAGE_WIDTH}")
    if mode not in {"ascii", "braille"}:
        raise ValueError("image glyph mode must be ascii or braille")
    _validate_source_dimensions(image)

    height = max(1, round(image.height / image.width * width * 0.5))
    if height > MAXIMUM_IMAGE_HEIGHT:
        width = max(1, round(width * MAXIMUM_IMAGE_HEIGHT / height))
        height = MAXIMUM_IMAGE_HEIGHT
    sample_size = (width, height) if mode == "ascii" else (width * 2, height * 4)
    sampled = image.convert("RGBA").resize(sample_size, Image.Resampling.LANCZOS)
    colors = _quantized_colors(sampled)
    if mode == "ascii":
        lines = _render_ascii(sampled, colors)
    else:
        lines = _render_braille(sampled, colors, width, height)

    for line in lines:
        if _SAFE_IMAGE_LINE.fullmatch(line) is None:
            raise ValueError("image renderer produced unsafe terminal output")
    return RenderedImage(tuple(lines), width, height, mode)


def _validate_source_dimensions(image: Image.Image) -> None:
    if image.width < 1 or image.height < 1:
        raise ValueError("image has invalid dimensions")
    if image.width * image.height > MAXIMUM_IMAGE_PIXELS:
        raise ValueError(f"image exceeds the {MAXIMUM_IMAGE_PIXELS} pixel limit")


def _quantized_colors(image: Image.Image) -> Image.Image:
    background = Image.new("RGB", image.size, "black")
    background.paste(image.convert("RGB"), mask=image.getchannel("A"))
    return background.quantize(
        colors=IMAGE_COLOR_LIMIT,
        method=Image.Quantize.MEDIANCUT,
    ).convert("RGB")


def _render_ascii(image: Image.Image, colors: Image.Image) -> list[str]:
    pixels = image.load()
    color_pixels = colors.load()
    lines = []
    for y in range(image.height):
        cells = []
        for x in range(image.width):
            red, green, blue, alpha = pixels[x, y]
            if alpha < 32:
                cells.append((" ", None))
                continue
            luminance = (299 * red + 587 * green + 114 * blue) // 1000
            glyph = _ASCII_GLYPHS[luminance * (len(_ASCII_GLYPHS) - 1) // 255]
            cells.append((glyph, _xterm_color(*color_pixels[x, y])))
        lines.append(_ansi_line(cells))
    return lines


def _render_braille(
    image: Image.Image,
    colors: Image.Image,
    width: int,
    height: int,
) -> list[str]:
    pixels = image.load()
    color_pixels = colors.load()
    lines = []
    for cell_y in range(height):
        cells = []
        for cell_x in range(width):
            bits = 0
            color_total = [0, 0, 0]
            color_samples = 0
            for offset_y, row in enumerate(_BRAILLE_DOTS):
                for offset_x, (bit, threshold) in enumerate(row):
                    x = cell_x * 2 + offset_x
                    y = cell_y * 4 + offset_y
                    red, green, blue, alpha = pixels[x, y]
                    luminance = (299 * red + 587 * green + 114 * blue) // 1000
                    if alpha >= 32 and luminance >= threshold:
                        bits |= bit
                        sample = color_pixels[x, y]
                        for index in range(3):
                            color_total[index] += sample[index]
                        color_samples += 1
            if not bits or not color_samples:
                cells.append((" ", None))
                continue
            color = tuple(value // color_samples for value in color_total)
            cells.append((chr(0x2800 + bits), _xterm_color(*color)))
        lines.append(_ansi_line(cells))
    return lines


def _ansi_line(cells: list[tuple[str, int | None]]) -> str:
    while cells and cells[-1] == (" ", None):
        cells.pop()
    output = []
    active_color = None
    for glyph, color in cells:
        if color is None:
            if active_color is not None:
                output.append("\x1b[0m")
                active_color = None
            output.append(glyph)
        else:
            if color != active_color:
                output.append(f"\x1b[38;5;{color}m")
                active_color = color
            output.append(glyph)
    if active_color is not None:
        output.append("\x1b[0m")
    return "".join(output)


def _xterm_color(red: int, green: int, blue: int) -> int:
    levels = (0, 95, 135, 175, 215, 255)
    indexes = tuple(
        min(range(6), key=lambda index: abs(levels[index] - value))
        for value in (red, green, blue)
    )
    cube = 16 + 36 * indexes[0] + 6 * indexes[1] + indexes[2]
    gray_index = min(23, max(0, round(((red + green + blue) / 3 - 8) / 10)))
    gray = 8 + gray_index * 10
    cube_error = sum(
        (value - levels[index]) ** 2
        for value, index in zip((red, green, blue), indexes, strict=True)
    )
    gray_error = sum((value - gray) ** 2 for value in (red, green, blue))
    return 232 + gray_index if gray_error < cube_error else cube
