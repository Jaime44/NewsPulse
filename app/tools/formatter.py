def format_email(summary: str, image_path: str) -> str:
    """
    Formatea un correo HTML atractivo con el resumen e imagen.
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
