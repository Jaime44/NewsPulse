import hmac
import os

from threading import Lock

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
from app.backend.bd.newsletter_source_store import (
    NewsletterSource,
    NewsletterSourceStore,
    NewsletterSourceStoreError,
)
from app.backend.services.oauth_credentials import (
    OAuthReauthenticationRequired,
    OAuthRefreshTemporarilyUnavailable,
    ensure_valid_credentials,
)
from app.backend.services.gmail_ingestion import (
    GmailIngestionError,
)
from app.backend.services.ingestion_factory import (
    build_gmail_ingestion_service,
)
from app.tools.logger import AppLogger
from app.config import AppConfig, read_secret_file
from app.tools.gmail.gmail_client import GmailClient


MANUAL_SCAN_TOTAL_LIMIT = 500
MANUAL_SCAN_PAGE_SIZE = 100

INGESTION_ACTION_HEADER = (
    "X-NewsPulse-Action"
)
INGESTION_ACTION_VALUE = "scan"

NEWSLETTER_SOURCE_ACTION_VALUE = "manage-source"

NEWSLETTER_SOURCE_PAYLOAD_FIELDS = frozenset(
    {
        "source_type",
        "source_value",
        "decision",
    }
)

NEWSLETTER_SOURCE_IDENTIFIER_FIELDS = frozenset(
    {
        "source_type",
        "source_value",
    }
)

ingestion_scan_lock = Lock()


logger = AppLogger("web_app.log")
logger.debug(f"START: ")

settings = AppConfig.load()

database = Database(settings.database_path)

oauth_store = OAuthCredentialStore(
    database=database,
    encryption_key_path=settings.token_encryption_key_path,
)
newsletter_source_store = NewsletterSourceStore(
    database
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

    if account_id is None:
        return None

    if account_id != OAuthCredentialStore.ACCOUNT_ID:
        logger.warning(
            "Rejected invalid account identifier from session"
        )
        session.clear()
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

def has_valid_action_header(
    expected_value: str,
) -> bool:
    """Validate one intentional browser action."""

    action_header = request.headers.get(
        INGESTION_ACTION_HEADER,
        "",
    )

    return hmac.compare_digest(
        action_header,
        expected_value,
    )

def serialize_newsletter_source(
    source: NewsletterSource,
) -> dict[str, object]:
    """Return the public API representation of one source."""

    return {
        "source_type": source.source_type,
        "source_value": source.source_value,
        "decision": source.decision,
        "origin": source.origin,
        "confidence": source.confidence,
        "active": source.active,
        "created_at": (
            source.created_at.isoformat()
        ),
        "updated_at": (
            source.updated_at.isoformat()
        ),
        "last_matched_at": (
            source.last_matched_at.isoformat()
            if source.last_matched_at
            is not None
            else None
        ),
    }

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
        
    except Exception as exc:
        logger.error(
            f"Google auth initiation failed: "
            f"{type(exc).__name__}"
        )
        return jsonify(
            {"error": "Authentication failed"}
        ), 500

@app.route('/auth/google/callback')
def google_callback():
    """Handle Google OAuth callback."""
    
    try:
        # Get authorization code from callback
        state = request.args.get("state")
        expected_state = session.pop(
            "oauth_state",
            None,
        )

        if (
            not state
            or not expected_state
            or not hmac.compare_digest(
                state,
                expected_state,
            )
        ):
            logger.error(
                "Invalid state parameter in OAuth callback"
            )
            return jsonify(
                {"error": "Invalid state parameter"}
            ), 400

        oauth_error = request.args.get("error")

        if oauth_error == "access_denied":
            logger.warning(
                "Google authorization was denied by the user"
            )
            return redirect(
                url_for(
                    "index",
                    error="google_authorization_denied",
                )
            )

        if oauth_error:
            logger.error(
                "Google returned an OAuth authorization error"
            )
            return redirect(
                url_for(
                    "index",
                    error="google_authorization_failed",
                )
            )

        code = request.args.get("code")

        if not code:
            logger.error(
                "Authorization code not received in callback"
            )
            return jsonify(
                {"error": "Authorization code not received"}
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
        
    except Exception as exc:
        logger.error(
            f"Google callback failed: "
            f"{type(exc).__name__}"
        )
        return jsonify(
            {"error": "Authentication callback failed"}
        ), 500

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

@app.get("/api/newsletter-sources")
def get_newsletter_sources():
    """List active newsletter source rules."""

    try:
        stored_account = (
            get_authenticated_account()
        )

        if stored_account is None:
            return jsonify(
                {"error": "Not authenticated"}
            ), 401

        sources = (
            newsletter_source_store
            .list_active_sources()
        )

        response = jsonify(
            {
                "sources": [
                    serialize_newsletter_source(
                        source
                    )
                    for source in sources
                ]
            }
        )
        response.headers["Cache-Control"] = (
            "no-store"
        )

        return response

    except NewsletterSourceStoreError as exc:
        logger.error(
            "Newsletter source listing failed: "
            f"{type(exc).__name__}"
        )
        return jsonify(
            {
                "error": (
                    "Newsletter sources unavailable"
                )
            }
        ), 500

    except Exception as exc:
        logger.error(
            "Newsletter source listing failed: "
            f"{type(exc).__name__}"
        )
        return jsonify(
            {
                "error": (
                    "Newsletter sources unavailable"
                )
            }
        ), 500

@app.post("/api/newsletter-sources")
def save_newsletter_source():
    """Create or update one manual newsletter source rule."""

    try:
        stored_account = (
            get_authenticated_account()
        )

        if stored_account is None:
            return jsonify(
                {"error": "Not authenticated"}
            ), 401

        if not has_valid_action_header(
            NEWSLETTER_SOURCE_ACTION_VALUE
        ):
            logger.warning(
                "Rejected invalid newsletter source request"
            )
            return jsonify(
                {
                    "error": (
                        "Invalid newsletter source request"
                    )
                }
            ), 403

        payload = request.get_json(
            silent=True
        )

        if (
            not isinstance(payload, dict)
            or set(payload)
            != NEWSLETTER_SOURCE_PAYLOAD_FIELDS
        ):
            return jsonify(
                {
                    "error": (
                        "Invalid newsletter source data"
                    )
                }
            ), 400

        source = newsletter_source_store.save_source(
            payload["source_type"],
            payload["source_value"],
            decision=payload["decision"],
            origin="manual",
            confidence=100,
        )

        response = jsonify(
            {
                "status": "saved",
                "source": (
                    serialize_newsletter_source(
                        source
                    )
                ),
            }
        )
        response.headers["Cache-Control"] = (
            "no-store"
        )

        return response

    except NewsletterSourceStoreError:
        return jsonify(
            {
                "error": (
                    "Invalid newsletter source data"
                )
            }
        ), 400

    except Exception as exc:
        logger.error(
            "Newsletter source save failed: "
            f"{type(exc).__name__}"
        )
        return jsonify(
            {
                "error": (
                    "Newsletter source could not be saved"
                )
            }
        ), 500

@app.post("/api/newsletter-sources/deactivate")
def deactivate_newsletter_source():
    """Deactivate one newsletter source without deleting it."""

    try:
        stored_account = (
            get_authenticated_account()
        )

        if stored_account is None:
            return jsonify(
                {"error": "Not authenticated"}
            ), 401

        if not has_valid_action_header(
            NEWSLETTER_SOURCE_ACTION_VALUE
        ):
            logger.warning(
                "Rejected invalid newsletter source request"
            )
            return jsonify(
                {
                    "error": (
                        "Invalid newsletter source request"
                    )
                }
            ), 403

        payload = request.get_json(
            silent=True
        )

        if (
            not isinstance(payload, dict)
            or set(payload)
            != NEWSLETTER_SOURCE_IDENTIFIER_FIELDS
        ):
            return jsonify(
                {
                    "error": (
                        "Invalid newsletter source data"
                    )
                }
            ), 400

        deactivated = (
            newsletter_source_store
            .deactivate_source(
                payload["source_type"],
                payload["source_value"],
            )
        )

        if not deactivated:
            return jsonify(
                {
                    "error": (
                        "Active newsletter source not found"
                    )
                }
            ), 404

        response = jsonify(
            {"status": "deactivated"}
        )
        response.headers["Cache-Control"] = (
            "no-store"
        )

        return response

    except NewsletterSourceStoreError:
        return jsonify(
            {
                "error": (
                    "Invalid newsletter source data"
                )
            }
        ), 400

    except Exception as exc:
        logger.error(
            "Newsletter source deactivation failed: "
            f"{type(exc).__name__}"
        )
        return jsonify(
            {
                "error": (
                    "Newsletter source could not "
                    "be deactivated"
                )
            }
        ), 500

@app.post("/api/ingestion/scan")
def run_gmail_ingestion():
    """Run one authenticated incremental Gmail scan."""

    scan_lock_acquired = False

    try:
        gmail_service = (
            get_authenticated_gmail_service()
        )

        if gmail_service is None:
            return jsonify(
                {"error": "Not authenticated"}
            ), 401

        if not has_valid_action_header(
            INGESTION_ACTION_VALUE
        ):
            logger.warning(
                "Rejected invalid manual ingestion request"
            )
            return jsonify(
                {"error": "Invalid ingestion request"}
            ), 403

        scan_lock_acquired = (
            ingestion_scan_lock.acquire(
                blocking=False
            )
        )

        if not scan_lock_acquired:
            return jsonify(
                {
                    "error": (
                        "An ingestion scan is "
                        "already running"
                    )
                }
            ), 409

        ingestion_service = (
            build_gmail_ingestion_service(
                gmail_service=gmail_service,
                database=database,
                retention_days=(
                    settings.retention_days
                ),
            )
        )

        result = ingestion_service.scan(
            user_id="me",
            total_limit=(
                MANUAL_SCAN_TOTAL_LIMIT
            ),
            page_size=(
                MANUAL_SCAN_PAGE_SIZE
            ),
        )

        return jsonify(
            {
                "status": "completed",
                "scan_started_at": (
                    result
                    .scan_started_at
                    .isoformat()
                ),
                "cursor_before": (
                    result
                    .cursor_before
                    .isoformat()
                ),
                "cursor_after": (
                    result
                    .cursor_after
                    .isoformat()
                ),
                "counts": {
                    "listed": (
                        result.listed_count
                    ),
                    "discovered": (
                        result.discovered_count
                    ),
                    "already_known": (
                        result.already_known_count
                    ),
                    "ignored_before_start": (
                        result
                        .ignored_before_start_count
                    ),
                    "newsletter": (
                        result.newsletter_count
                    ),
                    "review": (
                        result.review_count
                    ),
                    "not_newsletter": (
                        result
                        .not_newsletter_count
                    ),
                },
            }
        )

    except OAuthReauthenticationRequired:
        session.clear()
        logger.warning(
            "Google authorization must be "
            "granted again for ingestion"
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
            "Google credential refresh is "
            "temporarily unavailable for ingestion"
        )
        return jsonify(
            {
                "error": (
                    "Google authentication is "
                    "temporarily unavailable"
                )
            }
        ), 503

    except GmailIngestionError as exc:
        logger.error(
            "Manual Gmail ingestion failed: "
            f"{type(exc).__name__}"
        )
        return jsonify(
            {
                "error": (
                    "Gmail ingestion is "
                    "temporarily unavailable"
                )
            }
        ), 503

    except Exception as exc:
        logger.error(
            "Manual Gmail ingestion failed: "
            f"{type(exc).__name__}"
        )
        return jsonify(
            {"error": "Gmail ingestion failed"}
        ), 500

    finally:
        if scan_lock_acquired:
            ingestion_scan_lock.release()

@app.post("/logout")
def logout():
    """Clear the browser session."""

    session.clear()

    return redirect(
        url_for("index")
    )
