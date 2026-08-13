"""Input sanitization and prompt injection prevention."""


def sanitize_question(question: str, max_len: int = 2000) -> str:
    """Clean user question: length limit, remove control characters.

    Prevents oversized inputs and certain attack vectors.
    """
    if len(question) > max_len:
        raise ValueError(f"Question too long: {len(question)} > {max_len}")

    # Remove control characters and excessive whitespace
    sanitized = "".join(c for c in question if ord(c) >= 32 or c in "\n\t")
    return sanitized.strip()


def escape_for_prompt(text: str) -> str:
    """Escape text for safe inclusion in prompt.

    Prevents prompt break attacks via newlines, quotes, or triple-backticks.
    """
    # Replace problematic sequences
    text = text.replace('"""', '"\\"')  # Escape triple quotes
    text = text.replace("```", "` ` `")  # Break code fences
    text = text.replace("\n", " ")  # Replace newlines
    return text.strip()


def escape_metadata_list(items: list[str]) -> list[str]:
    """Escape metric/dimension/field names for safe prompt inclusion.

    Each item is cleaned to prevent prompt injection or format breaking.
    """
    return [escape_for_prompt(item) for item in items if item]
