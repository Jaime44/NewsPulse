#!/usr/bin/env python3
"""
Script to run the Smart Newsletters web application.
"""

import os
import sys

# Add the app directory to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))

if __name__ == '__main__':
    from app.backend.web_app import app
    print("🚀 Starting Smart Newsletters Web Application...")
    print("📧 Access the application at: http://localhost:5000")
    print("🔐 Google OAuth authentication will be available")
    print("=" * 50)
    
    app.run(debug=True, host='0.0.0.0', port=5000)

