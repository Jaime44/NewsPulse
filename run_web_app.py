#!/usr/bin/env python3
"""
Script to run the Smart Newsletters web application.
"""

from app.backend.web_app import app, settings

    
if __name__ == "__main__":
    print("🚀 Starting Smart Newsletters Web Application...")
    print("📧 Access the application at: http://localhost:5000")
    print("🔐 Google OAuth authentication will be available")
    print("=" * 50)
    app.run(
        host=settings.host,
        port=settings.port,
        debug=settings.debug,
    )

