"""Create the NewsPulse Flask application and expose its HTTP endpoints.

This module is the application's composition and delivery layer. At import
time it loads configuration, initializes SQLite-backed OAuth and newsletter
stores, configures secure session-cookie options, and registers routes for
Google OAuth, the dashboard, Gmail access, source management, manual ingestion,
and logout. OAuth credentials remain in encrypted server-side storage while the
browser session carries only transient state or the single account identifier.
"""

import hmac
import os

from threading import Lock
from time import perf_counter
from uuid import uuid4

from flask import (
    Flask,
    Response,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask.typing import ResponseReturnValue
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import Resource, build

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
    GmailIngestionAlreadyRunningError,
    GmailIngestionError,
)
from app.backend.services.ingestion_factory import (
    build_gmail_ingestion_service,
)
from app.tools.logger import (
    AppLogger,
    is_error_logged,
)
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


settings = AppConfig.load()

AppLogger.configure(
    log_directory=settings.log_directory,
    max_bytes=settings.log_max_bytes,
    backup_count=settings.log_backup_count,
    level=settings.log_level,
    timezone=settings.timezone,
)
logger = AppLogger("web_app.log")

logger.info(
    "Initializing NewsPulse application: environment=%s log_level=%s",
    settings.environment,
    settings.log_level,
)

persistence_started_at = perf_counter()
logger.debug(
    "Persistence initialization started"
)

try:
    database = Database(
        settings.database_path,
        busy_timeout_ms=(
            settings.sqlite_busy_timeout_ms
        ),
        logger=logger,
    )

    oauth_store = OAuthCredentialStore(
        database=database,
        encryption_key_path=settings.token_encryption_key_path,
    )
    newsletter_source_store = NewsletterSourceStore(
        database
    )
except Exception as exc:
    logger.critical(
        "Application startup aborted while initializing persistence: %s",
        type(exc).__name__,
        exc_info=True,
    )
    raise

logger.debug(
    "Persistence initialization completed: duration_ms=%.2f",
    (perf_counter() - persistence_started_at) * 1_000,
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

try:
    app.secret_key = read_secret_file(
        settings.flask_secret_key_path
    )
except Exception as exc:
    logger.critical(
        "Application startup aborted while loading the Flask secret: %s",
        type(exc).__name__,
        exc_info=True,
    )
    raise

logger.info(
    "NewsPulse application initialized successfully"
)


@app.before_request
def log_request_started() -> None:
    """Create request correlation context and record the HTTP boundary."""

    correlation_id = uuid4().hex
    g.log_correlation_token = (
        AppLogger.set_correlation_id(
            correlation_id
        )
    )
    g.request_started_at = perf_counter()
    logger.info(
        "HTTP request started: method=%s path=%s",
        request.method,
        request.path,
    )


@app.after_request
def log_request_completed(
    response: Response,
) -> Response:
    """Record HTTP status and duration without logging request contents.

    Args:
        response: Flask response produced for the current request.

    Returns:
        The unchanged response passed to the remaining Flask lifecycle.
    """

    started_at = getattr(
        g,
        "request_started_at",
        None,
    )
    duration_ms = (
        (perf_counter() - started_at) * 1_000
        if started_at is not None
        else 0.0
    )
    logger.info(
        "HTTP request completed: method=%s path=%s status=%d "
        "duration_ms=%.2f",
        request.method,
        request.path,
        response.status_code,
        duration_ms,
    )
    return response


@app.teardown_request
def clear_request_log_context(
    unhandled_error: BaseException | None,
) -> None:
    """Report unhandled failures and release request correlation context.

    Args:
        unhandled_error: Exception escaping request dispatch, when present.
    """

    if unhandled_error is not None:
        sanitized_error = RuntimeError(
            "Exception details redacted"
        )
        logger.error(
            "Unhandled HTTP request failure: %s",
            type(unhandled_error).__name__,
            exc_info=(
                RuntimeError,
                sanitized_error,
                unhandled_error.__traceback__,
            ),
        )

    correlation_token = getattr(
        g,
        "log_correlation_token",
        None,
    )

    if correlation_token is not None:
        AppLogger.reset_correlation_id(
            correlation_token
        )

def get_authenticated_account(
) -> StoredOAuthCredentials | None:
    """Load the session account from encrypted server-side storage.

    Returns:
        The active single-account authorization, or ``None`` when the session
        is absent, invalid, missing from storage, or revoked.

    Notes:
        Invalid, missing, and revoked account states clear the browser session.
    """

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

def get_authenticated_gmail_service() -> Resource | None:
    """Build a Gmail resource from valid server-side credentials.

    Returns:
        An authorized Gmail discovery resource, or ``None`` when no account is
        authenticated.

    Raises:
        OAuthReauthenticationRequired: If authorization is permanently invalid.
        OAuthRefreshTemporarilyUnavailable: If credentials cannot be refreshed
            because of a temporary Google or transport failure.
    """

    stored_account = get_authenticated_account()

    if stored_account is None:
        return None

    refresh_required = (
        not stored_account.credentials.valid
    )
    logger.debug(
        "Validating stored Google credentials: refresh_required=%s",
        refresh_required,
    )
    credentials = ensure_valid_credentials(
        account=stored_account,
        store=oauth_store,
    )

    if refresh_required:
        logger.info(
            "Google credentials refreshed and persisted successfully"
        )

    service_started_at = perf_counter()
    gmail_service = build(
        "gmail",
        "v1",
        credentials=credentials,
    )
    logger.debug(
        "Authenticated Gmail service built: duration_ms=%.2f",
        (perf_counter() - service_started_at) * 1_000,
    )
    return gmail_service

def has_valid_action_header(
    expected_value: str,
) -> bool:
    """Compare the request action header to an expected value in constant time.

    Args:
        expected_value: Exact action token required by the endpoint.

    Returns:
        Whether the request contains the expected action token.
    """

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
    """Return the public API representation of one source rule.

    Args:
        source: Stored newsletter source to expose.

    Returns:
        A JSON-compatible dictionary without internal row or account IDs.
    """

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
def index() -> str:
    """Render the public landing page with its Google sign-in control.

    Returns:
        Rendered landing-page HTML.
    """
    return render_template('index.html')

@app.route('/auth/google')
def google_auth() -> ResponseReturnValue:
    """Initiate Google OAuth and retain its CSRF state in the session.

    Returns:
        A redirect to Google's authorization page, or a sanitized JSON error.
    """
    
    try:
        # Create OAuth flow
        flow = Flow.from_client_secrets_file(
            str(settings.google_credentials_path),
            scopes=list(settings.google_scopes),
        )

        flow.redirect_uri = settings.google_redirect_uri
        logger.debug(
            "OAuth authorization flow created"
        )
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
            "Google auth initiation failed: "
            f"{type(exc).__name__}"
        )
        return jsonify(
            {"error": "Authentication failed"}
        ), 500

@app.route('/auth/google/callback')
def google_callback() -> ResponseReturnValue:
    """Validate and complete the Google OAuth callback.

    Returns:
        A dashboard or landing-page redirect on handled outcomes, or a
        sanitized JSON error for invalid or failed callbacks.

    Notes:
        Successful credentials are encrypted in SQLite before the session is
        replaced with the single server-side account identifier.
    """
    
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
            "Google callback failed: "
            f"{type(exc).__name__}"
        )
        return jsonify(
            {"error": "Authentication callback failed"}
        ), 500

@app.route('/dashboard')
def dashboard() -> ResponseReturnValue:
    """Render the dashboard for the connected account.

    Returns:
        Dashboard HTML, a landing-page redirect, or a sanitized storage error.
    """

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
def get_gmail_profile() -> ResponseReturnValue:
    """Return the authenticated Gmail profile as JSON.

    Returns:
        The profile response or a sanitized authentication/service error.
    """

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
        if is_error_logged(exc):
            logger.debug(
                "Gmail profile route received an already logged failure: %s",
                type(exc).__name__,
            )
        else:
            logger.error(
                "Failed to get Gmail profile: "
                f"{type(exc).__name__}"
            )
        return jsonify(
            {"error": "Failed to retrieve profile"}
        ), 500

@app.route("/api/gmail/messages")
def get_gmail_messages() -> ResponseReturnValue:
    """Return up to ten Gmail message references as JSON.

    Returns:
        A message collection or a sanitized authentication/service error.
    """

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
            "Failed to get Gmail messages: "
            f"{type(exc).__name__}"
        )
        return jsonify(
            {"error": "Failed to retrieve messages"}
        ), 500

@app.get("/api/newsletter-sources")
def get_newsletter_sources() -> ResponseReturnValue:
    """List active newsletter source rules for the authenticated account.

    Returns:
        A no-store JSON source collection or a sanitized error response.
    """

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

        logger.info(
            "Newsletter sources listed: count=%d",
            len(sources),
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
def save_newsletter_source() -> ResponseReturnValue:
    """Validate and create or update one manual newsletter source rule.

    Returns:
        A no-store JSON representation of the saved rule or a sanitized error.
    """

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

        logger.info(
            "Newsletter source saved: source_type=%s decision=%s",
            source.source_type,
            source.decision,
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
def deactivate_newsletter_source() -> ResponseReturnValue:
    """Validate and deactivate one newsletter source without deleting it.

    Returns:
        A JSON success status or a sanitized validation, lookup, or store error.
    """

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

        logger.info(
            "Newsletter source deactivated: source_type=%s",
            payload["source_type"],
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
def run_gmail_ingestion() -> ResponseReturnValue:
    """Run one non-concurrent authenticated incremental Gmail scan.

    Returns:
        A safe scan summary or a sanitized validation, authentication,
        concurrency, or service error.

    Notes:
        A process-local non-blocking lock prevents overlapping manual scans and
        is released in the ``finally`` block for every acquired execution.
    """

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
            logger.warning(
                "Manual ingestion request skipped because a scan is running"
            )
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
                ingestion_lease_seconds=(
                    settings.ingestion_lease_seconds
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

    except GmailIngestionAlreadyRunningError:
        return jsonify(
            {
                "error": (
                    "An ingestion scan is "
                    "already running"
                )
            }
        ), 409

    except GmailIngestionError as exc:
        if is_error_logged(exc):
            logger.debug(
                "Manual ingestion route received an already logged "
                "service failure: %s",
                type(exc).__name__,
            )
        else:
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
def logout() -> ResponseReturnValue:
    """Clear the browser session and redirect to the landing page.

    Returns:
        A redirect to the public landing page.
    """

    session.clear()

    logger.info(
        "User session logged out"
    )

    return redirect(
        url_for("index")
    )
