"""Test bounded retrieval and decoding of textual Gmail MIME content."""

from __future__ import annotations

import base64
import unittest

from unittest.mock import Mock

from app.backend.services.message_content import (
    DecodedTextPart,
    GmailMessageContentService,
    MessageContentError,
)
from app.tools.gmail.gmail_client import (
    GmailAttachmentData,
    GmailClient,
    GmailClientError,
    GmailFullMessage,
    GmailMimePart,
)


def encode_text(
    text: str,
    encoding: str = "utf-8",
) -> str:
    """Encode text as unpadded Gmail base64url data.

    Args:
        text: Text to encode.
        encoding: Character encoding applied before base64url conversion.

    Returns:
        The encoded Gmail body value.
    """

    encoded = base64.urlsafe_b64encode(
        text.encode(encoding)
    )

    return encoded.decode(
        "ascii"
    ).rstrip("=")


def make_part(
    mime_type: str,
    *,
    text_data: str | None = None,
    attachment_id: str | None = None,
    filename: str | None = None,
    headers: tuple[
        tuple[str, str],
        ...,
    ] = (),
    body_size: int = 0,
    parts: tuple[
        GmailMimePart,
        ...,
    ] = (),
) -> GmailMimePart:
    """Build a Gmail MIME part fixture.

    Args:
        mime_type: MIME type assigned to the part.
        text_data: Optional inline encoded body.
        attachment_id: Optional external Gmail body identifier.
        filename: Optional attachment filename.
        headers: Optional MIME headers.
        body_size: Declared body size.
        parts: Optional child parts.

    Returns:
        The immutable MIME part fixture.
    """

    return GmailMimePart(
        mime_type=mime_type,
        filename=filename,
        headers=headers,
        body_size=body_size,
        body_data=text_data,
        attachment_id=attachment_id,
        parts=parts,
    )


def make_message(
    root_part: GmailMimePart,
) -> GmailFullMessage:
    """Build a full Gmail message fixture around a root MIME part.

    Args:
        root_part: Root of the MIME tree.

    Returns:
        The immutable full-message fixture.
    """

    return GmailFullMessage(
        message_id="message-1",
        thread_id="thread-1",
        root_part=root_part,
    )


class GmailMessageContentServiceTests(
    unittest.TestCase
):
    """Verify safe MIME traversal, retrieval, and decoding."""

    def setUp(self) -> None:
        """Create isolated dependencies and fixtures for each test."""

        self.gmail_client = Mock(
            spec=GmailClient
        )
        self.service = (
            GmailMessageContentService(
                self.gmail_client
            )
        )

    def test_decodes_nested_inline_text_parts(
        self,
    ) -> None:
        """Verify that the service decodes nested inline text parts."""

        plain_text = "Hola desde texto"
        html_text = "<p>Café semanal</p>"

        root = make_part(
            "multipart/alternative",
            parts=(
                make_part(
                    "text/plain",
                    text_data=encode_text(
                        plain_text
                    ),
                    body_size=len(
                        plain_text.encode(
                            "utf-8"
                        )
                    ),
                ),
                make_part(
                    "text/html",
                    text_data=encode_text(
                        html_text,
                        "iso-8859-1",
                    ),
                    body_size=len(
                        html_text.encode(
                            "iso-8859-1"
                        )
                    ),
                    headers=(
                        (
                            "content-type",
                            (
                                "text/html; "
                                "charset=iso-8859-1"
                            ),
                        ),
                    ),
                ),
            ),
        )

        self.gmail_client.get_full_message.return_value = (
            make_message(root)
        )

        result = self.service.extract_text_parts(
            "message-1"
        )

        self.assertEqual(
            result,
            (
                DecodedTextPart(
                    mime_type="text/plain",
                    text=plain_text,
                ),
                DecodedTextPart(
                    mime_type="text/html",
                    text=html_text,
                ),
            ),
        )
        (
            self.gmail_client
            .get_attachment_data
            .assert_not_called()
        )

    def test_resolves_external_text_body(
        self,
    ) -> None:
        """Verify that the service resolves an external text body."""

        text = "Contenido externo"

        root = make_part(
            "text/plain",
            attachment_id="attachment-1",
            body_size=len(
                text.encode("utf-8")
            ),
        )

        self.gmail_client.get_full_message.return_value = (
            make_message(root)
        )
        (
            self.gmail_client
            .get_attachment_data
            .return_value
        ) = GmailAttachmentData(
            size=len(
                text.encode("utf-8")
            ),
            encoded_data=encode_text(
                text
            ),
        )

        result = self.service.extract_text_parts(
            "message-1"
        )

        self.assertEqual(
            result,
            (
                DecodedTextPart(
                    mime_type="text/plain",
                    text=text,
                ),
            ),
        )
        (
            self.gmail_client
            .get_attachment_data
            .assert_called_once_with(
                message_id="message-1",
                attachment_id="attachment-1",
                user_id="me",
            )
        )

    def test_skips_non_text_and_named_attachments(
        self,
    ) -> None:
        """Verify that non-text and named attachment parts are skipped."""

        root = make_part(
            "multipart/mixed",
            parts=(
                make_part(
                    "application/pdf",
                    attachment_id="pdf-1",
                    body_size=50,
                ),
                make_part(
                    "text/plain",
                    attachment_id="text-file-1",
                    filename="notes.txt",
                    body_size=20,
                ),
                make_part(
                    "text/plain",
                    text_data=encode_text(
                        "   "
                    ),
                    body_size=3,
                ),
            ),
        )

        self.gmail_client.get_full_message.return_value = (
            make_message(root)
        )

        result = self.service.extract_text_parts(
            "message-1"
        )

        self.assertEqual(
            result,
            (),
        )
        (
            self.gmail_client
            .get_attachment_data
            .assert_not_called()
        )

    def test_invalid_base64_is_rejected(
        self,
    ) -> None:
        """Verify that invalid base64 is rejected."""

        root = make_part(
            "text/plain",
            text_data="%%%",
            body_size=3,
        )

        self.gmail_client.get_full_message.return_value = (
            make_message(root)
        )

        with self.assertRaises(
            MessageContentError
        ) as raised:
            self.service.extract_text_parts(
                "message-1"
            )

        self.assertEqual(
            str(raised.exception),
            (
                "Gmail returned invalid "
                "encoded content"
            ),
        )

    def test_part_and_total_limits_are_enforced(
        self,
    ) -> None:
        """Verify that part and total limits are enforced."""

        four_bytes = make_part(
            "text/plain",
            text_data=encode_text(
                "1234"
            ),
            body_size=4,
        )

        cases = (
            {
                "max_part_bytes": 3,
                "max_total_bytes": 6,
                "parts": (
                    four_bytes,
                ),
            },
            {
                "max_part_bytes": 4,
                "max_total_bytes": 7,
                "parts": (
                    four_bytes,
                    four_bytes,
                ),
            },
        )

        for case in cases:
            with self.subTest(
                case=case
            ):
                gmail_client = Mock(
                    spec=GmailClient
                )
                service = (
                    GmailMessageContentService(
                        gmail_client,
                        max_part_bytes=(
                            case[
                                "max_part_bytes"
                            ]
                        ),
                        max_total_bytes=(
                            case[
                                "max_total_bytes"
                            ]
                        ),
                    )
                )

                root = make_part(
                    "multipart/mixed",
                    parts=case["parts"],
                )

                gmail_client.get_full_message.return_value = (
                    make_message(root)
                )

                with self.assertRaises(
                    MessageContentError
                ):
                    service.extract_text_parts(
                        "message-1"
                    )

    def test_unknown_charset_is_rejected(
        self,
    ) -> None:
        """Verify that unknown charset is rejected."""

        root = make_part(
            "text/plain",
            text_data=encode_text(
                "Hola"
            ),
            body_size=4,
            headers=(
                (
                    "content-type",
                    (
                        "text/plain; "
                        "charset=unknown-charset"
                    ),
                ),
            ),
        )

        self.gmail_client.get_full_message.return_value = (
            make_message(root)
        )

        with self.assertRaises(
            MessageContentError
        ) as raised:
            self.service.extract_text_parts(
                "message-1"
            )

        self.assertEqual(
            str(raised.exception),
            (
                "Gmail returned an "
                "unsupported charset"
            ),
        )

    def test_gmail_error_is_sanitized(
        self,
    ) -> None:
        """Verify that Gmail error is sanitized."""

        (
            self.gmail_client
            .get_full_message
            .side_effect
        ) = GmailClientError(
            "sensitive-provider-detail"
        )

        with self.assertRaises(
            MessageContentError
        ) as raised:
            self.service.extract_text_parts(
                "message-1"
            )

        self.assertEqual(
            str(raised.exception),
            (
                "Failed to retrieve "
                "Gmail message content"
            ),
        )
        self.assertNotIn(
            "sensitive-provider-detail",
            str(raised.exception),
        )

    def test_invalid_limits_are_rejected(
        self,
    ) -> None:
        """Verify that invalid limits are rejected."""

        invalid_limits = (
            {
                "max_part_bytes": 0,
                "max_total_bytes": 1,
            },
            {
                "max_part_bytes": True,
                "max_total_bytes": 1,
            },
            {
                "max_part_bytes": 2,
                "max_total_bytes": 1,
            },
            {
                "max_part_bytes": 1,
                "max_total_bytes": False,
            },
        )

        for limits in invalid_limits:
            with self.subTest(
                limits=limits
            ):
                with self.assertRaises(
                    ValueError
                ):
                    GmailMessageContentService(
                        self.gmail_client,
                        **limits,
                    )


if __name__ == "__main__":
    unittest.main()
