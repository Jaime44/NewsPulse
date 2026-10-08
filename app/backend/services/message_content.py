from __future__ import annotations

import base64
import binascii
import codecs

from collections.abc import Iterator
from dataclasses import dataclass
from email.message import Message

from app.tools.gmail.gmail_client import (
    GmailClient,
    GmailClientError,
    GmailMimePart,
)


class MessageContentError(RuntimeError):
    """Raised when Gmail content cannot be decoded safely."""


@dataclass(frozen=True, slots=True)
class DecodedTextPart:
    """One decoded textual MIME part."""

    mime_type: str
    text: str


def _walk_mime_parts(
    part: GmailMimePart,
) -> Iterator[GmailMimePart]:
    """Yield one MIME part and all its descendants."""

    yield part

    for child in part.parts:
        yield from _walk_mime_parts(
            child
        )


def _decode_base64url(
    encoded_data: str,
) -> bytes:
    """Decode validated Gmail base64url data."""

    try:
        encoded_bytes = encoded_data.encode(
            "ascii"
        )
    except UnicodeEncodeError as exc:
        raise MessageContentError(
            "Gmail returned invalid encoded content"
        ) from exc

    padding = b"=" * (
        -len(encoded_bytes) % 4
    )

    try:
        return base64.b64decode(
            encoded_bytes + padding,
            altchars=b"-_",
            validate=True,
        )
    except (
        binascii.Error,
        ValueError,
    ) as exc:
        raise MessageContentError(
            "Gmail returned invalid encoded content"
        ) from exc


def _find_header(
    part: GmailMimePart,
    header_name: str,
) -> str | None:
    """Return the first normalized MIME header value."""

    normalized_name = (
        header_name.strip().casefold()
    )

    for name, value in part.headers:
        if name == normalized_name:
            return value

    return None


def _get_charset(
    part: GmailMimePart,
) -> str:
    """Resolve and validate the part character encoding."""

    content_type = _find_header(
        part,
        "content-type",
    )

    if content_type is None:
        return "utf-8"

    descriptor = Message()
    descriptor["content-type"] = content_type

    charset = (
        descriptor.get_content_charset()
        or "utf-8"
    )

    try:
        return codecs.lookup(
            charset
        ).name
    except LookupError as exc:
        raise MessageContentError(
            "Gmail returned an unsupported charset"
        ) from exc


class GmailMessageContentService:
    """Retrieve and safely decode textual Gmail MIME parts."""

    TEXT_MIME_TYPES = frozenset(
        {
            "text/plain",
            "text/html",
        }
    )

    DEFAULT_MAX_PART_BYTES = 1_000_000
    DEFAULT_MAX_TOTAL_BYTES = 5_000_000

    def __init__(
        self,
        gmail_client: GmailClient,
        *,
        max_part_bytes: int = (
            DEFAULT_MAX_PART_BYTES
        ),
        max_total_bytes: int = (
            DEFAULT_MAX_TOTAL_BYTES
        ),
    ) -> None:
        if (
            isinstance(max_part_bytes, bool)
            or not isinstance(
                max_part_bytes,
                int,
            )
            or max_part_bytes <= 0
        ):
            raise ValueError(
                "max_part_bytes must be "
                "a positive integer"
            )

        if (
            isinstance(max_total_bytes, bool)
            or not isinstance(
                max_total_bytes,
                int,
            )
            or max_total_bytes
            < max_part_bytes
        ):
            raise ValueError(
                "max_total_bytes must be an integer "
                "greater than or equal to "
                "max_part_bytes"
            )

        self.gmail_client = gmail_client
        self.max_part_bytes = (
            max_part_bytes
        )
        self.max_total_bytes = (
            max_total_bytes
        )

    def _load_encoded_data(
        self,
        part: GmailMimePart,
        *,
        message_id: str,
        user_id: str,
    ) -> str | None:
        """Load inline data or one external Gmail body."""

        if (
            part.body_data is not None
            and part.body_data != ""
        ):
            return part.body_data

        if part.attachment_id is None:
            return None

        attachment = (
            self.gmail_client
            .get_attachment_data(
                message_id=message_id,
                attachment_id=(
                    part.attachment_id
                ),
                user_id=user_id,
            )
        )

        if attachment.size > self.max_part_bytes:
            raise MessageContentError(
                "Gmail text part exceeds "
                "the configured size limit"
            )

        return attachment.encoded_data

    def extract_text_parts(
        self,
        message_id: str,
        user_id: str = "me",
    ) -> tuple[DecodedTextPart, ...]:
        """Retrieve and decode textual, non-file MIME parts."""

        try:
            message = (
                self.gmail_client
                .get_full_message(
                    message_id=message_id,
                    user_id=user_id,
                )
            )

            decoded_parts: list[
                DecodedTextPart
            ] = []
            total_decoded_bytes = 0

            for part in _walk_mime_parts(
                message.root_part
            ):
                if (
                    part.mime_type
                    not in self.TEXT_MIME_TYPES
                    or part.filename is not None
                ):
                    continue

                if (
                    part.body_size
                    > self.max_part_bytes
                ):
                    raise MessageContentError(
                        "Gmail text part exceeds "
                        "the configured size limit"
                    )

                encoded_data = (
                    self._load_encoded_data(
                        part,
                        message_id=(
                            message.message_id
                        ),
                        user_id=user_id,
                    )
                )

                if encoded_data is None:
                    continue

                decoded_bytes = (
                    _decode_base64url(
                        encoded_data
                    )
                )

                if (
                    len(decoded_bytes)
                    > self.max_part_bytes
                ):
                    raise MessageContentError(
                        "Gmail text part exceeds "
                        "the configured size limit"
                    )

                total_decoded_bytes += len(
                    decoded_bytes
                )

                if (
                    total_decoded_bytes
                    > self.max_total_bytes
                ):
                    raise MessageContentError(
                        "Gmail text content exceeds "
                        "the configured total limit"
                    )

                charset = _get_charset(
                    part
                )

                text = decoded_bytes.decode(
                    charset,
                    errors="replace",
                ).replace(
                    "\x00",
                    "",
                )

                if not text.strip():
                    continue

                decoded_parts.append(
                    DecodedTextPart(
                        mime_type=(
                            part.mime_type
                        ),
                        text=text,
                    )
                )

            return tuple(decoded_parts)

        except GmailClientError as exc:
            raise MessageContentError(
                "Failed to retrieve "
                "Gmail message content"
            ) from exc