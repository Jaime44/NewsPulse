"""Provide the placeholder summarization boundary for newsletter content."""


def summarize_text(text: str) -> str:
    """Return the current fixed placeholder summary.

    Args:
        text: Source text reserved for a future summarization model.

    Returns:
        The existing Spanish placeholder string, regardless of the input.
    """
    # TODO: Integrate a BART or T5 summarization model.
    return "Resumen del texto"
