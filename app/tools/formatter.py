"""Build the simple HTML email body used by the prototype sending pipeline."""


def format_email(summary: str, image_path: str) -> str:
    """Embed a summary and image path in the prototype HTML template.

    Args:
        summary: Text to place in the message paragraph.
        image_path: Value assigned to the image ``src`` attribute.

    Returns:
        The rendered HTML fragment.
    """
    html = f"""
    <html>
      <body>
        <img src="{image_path}" alt="Imagen representativa" />
        <p>{summary}</p>
      </body>
    </html>
    """
    return html
