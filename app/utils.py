import random
import re


_SPINTAX_BLOCK = re.compile(r"\{([^{}]*)\}")


def parse_spintax(text: str) -> str:
    """Replace valid `{option one|option two}` blocks with a random option.

    Blocks without alternatives, empty alternatives, or unmatched braces are
    preserved so malformed user input never prevents a webhook response.
    """
    if not isinstance(text, str) or not text:
        return text

    def replace_block(match: re.Match[str]) -> str:
        options = match.group(1).split("|")
        if len(options) < 2 or any(not option.strip() for option in options):
            return match.group(0)
        return random.choice(options)

    return _SPINTAX_BLOCK.sub(replace_block, text)
