import os
import sys
import json

# Add the app directory to the Python path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from dotenv import load_dotenv
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from flask import Flask, render_template, request, jsonify, session, redirect, url_for


from app.tools import utils as Utils
from app.tools.logger import AppLogger
from app.tools.gmail.gmail_client import GmailClient
from app.tools.gmail.gmail_authenticator import GmailAuthenticator

Utils.clear_files_in_directory()
    
logger = AppLogger("web_app.log")
logger.debug(f"START: ")

def load_environment_variables(env_path: str = ".env") -> dict:
    """Load and prepare environment variables from a .env file."""
    logger.debug(f"Loading environment variables from {env_path}")
    if not os.path.exists(env_path):
        raise FileNotFoundError(f".env file not found at path: {env_path}")
    
    load_dotenv(dotenv_path=env_path)
    
    try:
        creds_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_WEB_APP")
        scopes = os.getenv("SCOPES")
        scopes = [s.strip() for s in scopes.split(",")] 

        if not creds_path or not scopes:
            raise KeyError("One or more required environment variables are missing")
        if not creds_path:
            raise KeyError("GOOGLE_APPLICATION_CREDENTIALS_WEB_APP is missing")
        return {
            "creds_path": str(creds_path),
            "scopes": scopes
        }
    except KeyError as e:
        raise KeyError(f"Error loading environment variables: {e}")

# Load environment variables
try:
    env_vars = load_environment_variables(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", ".env")
    )
    logger.info(f"Environment variables loaded: {env_vars}")
except Exception as e:
    _,_, exec_tb = sys.exc_info()
    line_number = exec_tb.tb_lineno if exec_tb else 'unknown'
    function_name = exec_tb.tb_frame.f_code.co_name if exec_tb else 'unknown'
    logger.error(f"Failed in '{function_name}' at line '{line_number}' to load environment variables: : {e}")
    env_vars = None
    
app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), '../frontend/templates'))

secret_key_path = os.getenv("SECRET_KEY_PATH")
if not secret_key_path:
    raise KeyError("SECRET_KEY_FILE is missing")
if not os.path.exists(secret_key_path):
        raise FileNotFoundError(f".env file not found at path: {secret_key_path}")
else:
    app.secret_key = Utils.load_secret_key_from_file(secret_key_path)

@app.route('/')
def index():
    """Serve the main page with Google Sign-In button."""
    return render_template('index.html')

@app.route('/auth/google')
def google_auth():
    """Initiate Google OAuth flow."""
    if not env_vars:
        return jsonify({"error": "Server configuration error"}), 500
    
    try:
        logger.debug(f"SCOPE: {env_vars['scopes']}")

        # Create OAuth flow
        flow = Flow.from_client_secrets_file(
            env_vars["creds_path"],
            scopes=env_vars["scopes"]
        )
        flow.redirect_uri = url_for('google_callback', _external=True)
        logger.debug(f"OAuth flow created with redirect URI: {flow.redirect_uri}")
        # Generate authorization URL
        authorization_url, state = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true'
        )
        
        # Store state in session
        session['oauth_state'] = state
        
        return redirect(authorization_url)
        
    except Exception as e:
        logger.error(f"Google auth initiation failed: {e}")
        return jsonify({"error": "Authentication failed"}), 500

@app.route('/auth/google/callback')
def google_callback():
    """Handle Google OAuth callback."""
    if not env_vars:
        return jsonify({"error": "Server configuration error"}), 500
    
    try:
        # Get authorization code from callback
        code = request.args.get('code')
        state = request.args.get('state')
        
        if not code:
            logger.error("Authorization code not received in callback")
            return jsonify({"error": "Authorization code not received"}), 400
        
        # Verify state
        if state != session.get('oauth_state'):
            logger.error("Invalid state parameter in OAuth callback")
            return jsonify({"error": "Invalid state parameter"}), 400
                
        # Create OAuth flow
        flow = Flow.from_client_secrets_file(
            env_vars["creds_path"],
            scopes=env_vars["scopes"]
        )
        flow.redirect_uri = url_for('google_callback', _external=True)
        
        # Exchange code for credentials
        flow.fetch_token(code=code)
        credentials = flow.credentials
        
        # Store credentials in session (in production, store securely)
        session['credentials'] = {
            'token': credentials.token,
            'refresh_token': credentials.refresh_token,
            # 'token_uri': credentials.token_uri,
            'client_id': credentials.client_id,
            'client_secret': credentials.client_secret,
            'scopes': credentials.scopes
        }
        
        # Create Gmail service and get user info
        gmail_service = build('gmail', 'v1', credentials=credentials)
        gmail_client = GmailClient(gmail_service)
        profile = gmail_client.get_profile()
        
        session['user_email'] = profile.get('emailAddress')
        session['user_name'] = profile.get('name', '')
        
        logger.info(f"User authenticated successfully: {session['user_email']}")
        
        return redirect(url_for('dashboard'))
        
    except Exception as e:
        logger.error(f"Google callback failed: {e}")
        return jsonify({"error": "Authentication callback failed"}), 500

@app.route('/dashboard')
def dashboard():
    """User dashboard after successful authentication."""
    if 'user_email' not in session:
        return redirect(url_for('index'))
    
    return render_template('dashboard.html', 
                         user_email=session['user_email'],
                         user_name=session.get('user_name', ''))

@app.route('/api/gmail/profile')
def get_gmail_profile():
    """Get Gmail profile using stored credentials."""
    if 'credentials' not in session:
        return jsonify({"error": "Not authenticated"}), 401
    
    try:
        # Recreate credentials from session
        creds_data = session['credentials']
        credentials = Credentials(
            token=creds_data['token'],
            refresh_token=creds_data['refresh_token'],
            token_uri=creds_data['token_uri'],
            client_id=creds_data['client_id'],
            client_secret=creds_data['client_secret'],
            scopes=creds_data['scopes']
        )
        
        # Create Gmail service
        gmail_service = build('gmail', 'v1', credentials=credentials)
        gmail_client = GmailClient(gmail_service)
        profile = gmail_client.get_profile()
        
        return jsonify(profile)
        
    except Exception as e:
        logger.error(f"Failed to get Gmail profile: {e}")
        return jsonify({"error": "Failed to retrieve profile"}), 500

@app.route('/api/gmail/messages')
def get_gmail_messages():
    """Get Gmail messages using stored credentials."""
    if 'credentials' not in session:
        return jsonify({"error": "Not authenticated"}), 401
    
    try:
        # Recreate credentials from session
        creds_data = session['credentials']
        credentials = Credentials(
            token=creds_data['token'],
            refresh_token=creds_data['refresh_token'],
            token_uri=creds_data['token_uri'],
            client_id=creds_data['client_id'],
            client_secret=creds_data['client_secret'],
            scopes=creds_data['scopes']
        )
        
        # Create Gmail service
        gmail_service = build('gmail', 'v1', credentials=credentials)
        
        # Get messages (example: last 10 messages)
        results = gmail_service.users().messages().list(userId='me', maxResults=10).execute()
        messages = results.get('messages', [])
        
        return jsonify({"messages": messages})
        
    except Exception as e:
        logger.error(f"Failed to get Gmail messages: {e}")
        return jsonify({"error": "Failed to retrieve messages"}), 500

@app.route('/logout')
def logout():
    """Logout user and clear session."""
    session.clear()
    return redirect(url_for('index'))

if __name__ == '__main__':
    # Clear log files on startup
    app.run(debug=True, host='0.0.0.0', port=5000)

