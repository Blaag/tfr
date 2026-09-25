from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image

from tfr.ansi import strip_ansi
from tfr.image_art import (
    MAXIMUM_IMAGE_HEIGHT,
    MAXIMUM_IMAGE_PIXELS,
    MAXIMUM_IMAGE_WIDTH,
    load_image_file,
    render_image,
)


def test_ascii_render_is_aspect_correct_bounded_and_grayscale_by_default() -> None:
    image = Image.new("RGB", (400, 200), (255, 0, 0))

    rendered = render_image(image, width=72, mode="ascii")

    assert rendered.width == 72
    assert rendered.height == 18
    assert len(rendered.lines) == 18
    assert all(len(strip_ansi(line)) == 72 for line in rendered.lines)
    assert all("\x1b[" not in line for line in rendered.lines)
    assert all(0x20 <= ord(character) <= 0x7E for line in rendered.lines for character in line)


def test_ascii_render_can_include_xterm_color() -> None:
    rendered = render_image(
        Image.new("RGB", (4, 2), (255, 0, 0)),
        width=4,
        mode="ascii",
        with_color=True,
    )

    assert rendered.lines[0].endswith("\x1b[0m")
    assert "\x1b[38;5;" in rendered.lines[0]


def test_braille_render_uses_unicode_cells_and_transparent_blanks() -> None:
    image = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
    for y in range(4):
        image.putpixel((0, y), (255, 255, 255, 255))

    rendered = render_image(image, width=2, mode="braille")

    plain = strip_ansi(rendered.lines[0])
    assert rendered.height == 1
    assert plain[0] == "⡇"
    assert plain[1:] == ""
    assert "\x1b[38;5;" in rendered.lines[0]


def test_render_rejects_unbounded_geometry_and_source_work() -> None:
    image = Image.new("RGB", (1, 1))
    with pytest.raises(ValueError, match="between 1 and 80"):
        render_image(image, width=MAXIMUM_IMAGE_WIDTH + 1)

    oversized = Image.new("1", (MAXIMUM_IMAGE_PIXELS + 1, 1))
    with pytest.raises(ValueError, match="pixel limit"):
        render_image(oversized)


def test_tall_image_is_capped_at_maximum_height() -> None:
    rendered = render_image(Image.new("RGB", (1, 100), "white"), width=72)
    assert rendered.height == MAXIMUM_IMAGE_HEIGHT
    assert rendered.width == 1


def test_file_loader_uses_first_frame_and_rejects_non_images(tmp_path: Path) -> None:
    path = tmp_path / "animated.gif"
    Image.new("RGB", (3, 2), "red").save(
        path,
        save_all=True,
        append_images=[Image.new("RGB", (3, 2), "blue")],
    )
    loaded = load_image_file(path)
    assert loaded.size == (3, 2)
    assert loaded.getpixel((0, 0))[:3] == (255, 0, 0)

    invalid = tmp_path / "not-image.txt"
    invalid.write_text("hello", encoding="utf-8")
    with pytest.raises(ValueError, match="supported image"):
        load_image_file(invalid)


def test_file_loader_rejects_formats_with_external_decoders(tmp_path: Path) -> None:
    path = tmp_path / "image.eps"
    Image.new("RGB", (3, 2), "red").save(path)

    with pytest.raises(ValueError, match="format EPS is not supported"):
        load_image_file(path)


def test_file_loader_rejects_non_regular_files(tmp_path: Path) -> None:
    fifo = tmp_path / "image-fifo"
    os.mkfifo(fifo)

    with pytest.raises(ValueError, match="regular file"):
        load_image_file(fifo)
