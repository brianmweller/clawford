"""Convert known names to Obsidian [[wikilinks]]."""

import re

from config import WIKILINK_ALIASES


def apply_wikilinks(text: str) -> str:
    """Replace known names with [[slug|Name]] wikilinks.

    Only matches at word boundaries. Skips names already inside [[ ]].
    """
    for name, slug in WIKILINK_ALIASES.items():
        wikilink = f"[[{slug}|{name}]]"
        # Match the name at word boundaries, but NOT if preceded by | or [
        # This avoids double-linking names already inside wikilinks.
        pattern = rf"(?<!\|)(?<!\[)\b{re.escape(name)}\b"
        text = re.sub(pattern, wikilink, text)
    return text
