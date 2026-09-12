import hmac
import os
import sys

sys.path.append(
    os.path.join(
        os.path.dirname(__file__),
        "..",
    )
)

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from app.backend.bd.db import Database
from app.backend.bd.oauth_store import (
    OAuthCredentialStore,
    StoredOAuthCredentials,
)
from app.backend.services.oauth_credentials import (
    OAuthReauthenticationRequired,
    OAuthRefreshTemporarilyUnavailable,
    ensure_valid_credentials,
)
from app.tools.logger import AppLogger
from app.config import AppConfig, read_secret_file
from app.tools.gmail.gmail_client import GmailClient


  
logger = AppLogger("web_app.log")
logger.debug(f"START: ")

settings = AppConfig.load()

database = Database(settings.database_path)

oauth_store = OAuthCredentialStore(
    database=database,
    encryption_key_path=settings.token_encryption_key_path,
)

app = Flask(
    __name__,
    template_folder=os.path.join(
        os.path.dirname(__file__),
        "../frontend/templates",
    ),
)

is_production = (
    settings.environment.strip().lower()
    == "production"
)

app.config.update(
    SESSION_COOKIE_NAME="newspulse_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=is_production,
    SESSION_COOKIE_SAMESITE="Lax",
)

app.secret_key = read_secret_file(
    settings.flask_secret_key_path
)

def get_authenticated_account(
) -> StoredOAuthCredentials | None:
    """Load the authenticated account from server-side storage."""

    account_id = session.get("account_id")

    if account_id != OAuthCredentialStore.ACCOUNT_ID:
        return None

    stored_account = oauth_store.load()

    if stored_account is None or stored_account.revoked:
        session.clear()
        return None

    return stored_account

def get_authenticated_gmail_service():
    """Build Gmail using valid server-side credentials."""

    stored_account = get_authenticated_account()

    if stored_account is None:
        return None

    credentials = ensure_valid_credentials(
        account=stored_account,
        store=oauth_store,
    )

    return build(
        "gmail",
        "v1",
        credentials=credentials,
    )

@app.route('/')
def index():
    """Serve the main page with Google Sign-In button."""
    return render_template('index.html')

@app.route('/auth/google')
def google_auth():
    """Initiate Google OAuth flow."""
    
    try:
        # Create OAuth flow
        flow = Flow.from_client_secrets_file(
            str(settings.google_credentials_path),
            scopes=list(settings.google_scopes),
        )

        flow.redirect_uri = settings.google_redirect_uri
        logger.debug(f"OAuth flow created with redirect URI: {flow.redirect_uri}")
        # Generate authorization URL
        authorization_url, state = flow.authorization_url(
            access_type="offline",
            prompt="consent",
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
    
    try:
        # Get authorization code from callback
        code = request.args.get('code')
        state = request.args.get("state")
        expected_state = session.pop("oauth_state", None)
        
        if not code:
            logger.error("Authorization code not received in callback")
            return jsonify({"error": "Authorization code not received"}), 400
        
        # Verify state
        if (
            not state
            or not expected_state
            or not hmac.compare_digest(state, expected_state)
        ):
            logger.error("Invalid state parameter in OAuth callback")
            return jsonify(
                {"error": "Invalid state parameter"}
            ), 400
                
        # Create OAuth flow
        flow = Flow.from_client_secrets_file(
            str(settings.google_credentials_path),
            scopes=list(settings.google_scopes),
        )

        flow.redirect_uri = settings.google_redirect_uri
        
        # Exchange code for credentials
        flow.fetch_token(code=code)
        credentials = flow.credentials
        
        # Create Gmail service and get user info
        gmail_service = build('gmail', 'v1', credentials=credentials)
        gmail_client = GmailClient(gmail_service)
        
        profile = gmail_client.get_profile()

        email = profile.get("emailAddress")

        if not email:
            raise RuntimeError(
                "Google did not return the Gmail email address"
            )

        oauth_store.save(
            email=email,
            credentials=credentials,
        )

        session.clear()
        session["account_id"] = OAuthCredentialStore.ACCOUNT_ID

        logger.info(
            "Google account authenticated successfully"
        )
        
        return redirect(url_for('dashboard'))
        
    except Exception as e:
        logger.error(f"Google callback failed: {e}")
        return jsonify({"error": "Authentication callback failed"}), 500

@app.route('/dashboard')
def dashboard():
    """Display the dashboard for the connected account."""

    try:
        stored_account = get_authenticated_account()
    except Exception:
        logger.error(
            "Stored OAuth credentials could not be loaded"
        )
        session.clear()
        return jsonify(
            {"error": "Stored credentials are unavailable"}
        ), 500

    if stored_account is None:
        return redirect(url_for("index"))

    return render_template(
        "dashboard.html",
        user_email=stored_account.email,
        user_name="",
    )

@app.route("/api/gmail/profile")
def get_gmail_profile():
    """Get the Gmail profile using valid server-side credentials."""

    try:
        gmail_service = get_authenticated_gmail_service()

        if gmail_service is None:
            return jsonify(
                {"error": "Not authenticated"}
            ), 401

        gmail_client = GmailClient(gmail_service)
        profile = gmail_client.get_profile()

        return jsonify(profile)

    except OAuthReauthenticationRequired:
        session.clear()
        logger.warning(
            "Google authorization must be granted again"
        )
        return jsonify(
            {
                "error": (
                    "Google authorization expired; "
                    "authenticate again"
                )
            }
        ), 401

    except OAuthRefreshTemporarilyUnavailable:
        logger.warning(
            "Google credential refresh is temporarily unavailable"
        )
        return jsonify(
            {
                "error": (
                    "Google authentication is "
                    "temporarily unavailable"
                )
            }
        ), 503

    except Exception as exc:
        logger.error(
            f"Failed to get Gmail profile: "
            f"{type(exc).__name__}"
        )
        return jsonify(
            {"error": "Failed to retrieve profile"}
        ), 500

@app.route("/api/gmail/messages")
def get_gmail_messages():
    """List Gmail messages using valid server-side credentials."""

    try:
        gmail_service = get_authenticated_gmail_service()

        if gmail_service is None:
            return jsonify(
                {"error": "Not authenticated"}
            ), 401

        results = (
            gmail_service
            .users()
            .messages()
            .list(
                userId="me",
                maxResults=10,
            )
            .execute()
        )

        messages = results.get("messages", [])

        return jsonify({"messages": messages})

    except OAuthReauthenticationRequired:
        session.clear()
        logger.warning(
            "Google authorization must be granted again"
        )
        return jsonify(
            {
                "error": (
                    "Google authorization expired; "
                    "authenticate again"
                )
            }
        ), 401

    except OAuthRefreshTemporarilyUnavailable:
        logger.warning(
            "Google credential refresh is temporarily unavailable"
        )
        return jsonify(
            {
                "error": (
                    "Google authentication is "
                    "temporarily unavailable"
                )
            }
        ), 503

    except Exception as exc:
        logger.error(
            f"Failed to get Gmail messages: "
            f"{type(exc).__name__}"
        )
        return jsonify(
            {"error": "Failed to retrieve messages"}
        ), 500

@app.route('/logout')
def logout():
    """Logout user and clear session."""
    session.clear()
    return redirect(url_for('index'))
