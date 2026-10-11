#!/usr/bin/env python3
"""Run the configured NewsPulse Flask development server.

Importing :mod:`app.backend.web_app` constructs the application and its local
stores. Executing this module starts Flask with the validated host, port, and
debug settings.
"""

from app.backend.web_app import app, logger, settings


if __name__ == "__main__":
    print("🚀 Starting News Pulse Web Application...")
    print("📧 Access the application at: http://localhost:5000")
    print("🔐 Google OAuth authentication will be available")
    print("=" * 50)
    logger.info(
        "Starting Flask development server: host=%s port=%d debug=%s",
        settings.host,
        settings.port,
        settings.debug,
    )
    app.run(
        host=settings.host,
        port=settings.port,
        debug=settings.debug,
    )
