from __future__ import annotations

_ESCAPED_CHARACTERS = {
    "bare": frozenset(),
    "tinymush": frozenset("%;[]{}\\"),
    "tinymux": frozenset("[]{}(),;#%\\"),
}


def escape_world_text(text: str, server: str) -> str:
    if server == "bare":
        return text
    try:
        escaped_characters = _ESCAPED_CHARACTERS[server]
    except KeyError as exc:
        raise ValueError("text escaping requires a bare, tinymush, or tinymux world") from exc

    escaped: list[str] = []
    for character in text:
        if character == " ":
            escaped.append("%b")
        elif character == "\t":
            escaped.append("%t")
        elif character in escaped_characters:
            escaped.extend(("\\", character))
        else:
            escaped.append(character)
    return "".join(escaped)
