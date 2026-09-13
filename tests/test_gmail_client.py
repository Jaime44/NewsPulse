from __future__ import annotations

import unittest

from unittest.mock import Mock, call

from app.tools.gmail.gmail_client import (
    GmailClient,
    GmailClientError,
    GmailMessageMetadata,
    GmailMessageReference,
)
from app.tools.gmail.messages_client import (
    MessagesClientError,
)


class GmailClientPaginationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = GmailClient.__new__(
            GmailClient
        )
        self.client.messages = Mock()
        self.client.logger = Mock()

        self.list_page = (
            self.client
            .messages
            .list_messages
        )

    def test_single_page_returns_typed_references(
        self,
    ) -> None:
        self.list_page.return_value = {
            "messages": [
                {
                    "id": " message-1 ",
                    "threadId": " thread-1 ",
                }
            ]
        }

        result = self.client.list_message_references(
            user_id="me",
            total_limit=10,
            page_size=5,
            query="after:1757721600",
            label_ids=("INBOX",),
        )

        self.assertEqual(
            result,
            [
                GmailMessageReference(
                    message_id="message-1",
                    thread_id="thread-1",
                )
            ],
        )

        self.list_page.assert_called_once_with(
            user_id="me",
            max_results=5,
            page_token=None,
            query="after:1757721600",
            label_ids=("INBOX",),
            include_spam_trash=False,
        )

    def test_multiple_pages_use_next_page_token(
        self,
    ) -> None:
        self.list_page.side_effect = [
            {
                "messages": [
                    {
                        "id": "message-1",
                        "threadId": "thread-1",
                    },
                    {
                        "id": "message-2",
                        "threadId": "thread-2",
                    },
                ],
                "nextPageToken": "page-2",
            },
            {
                "messages": [
                    {
                        "id": "message-3",
                        "threadId": "thread-3",
                    }
                ]
            },
        ]

        result = self.client.list_message_references(
            total_limit=3,
            page_size=2,
        )

        self.assertEqual(
            [
                reference.message_id
                for reference in result
            ],
            [
                "message-1",
                "message-2",
                "message-3",
            ],
        )

        self.assertEqual(
            self.list_page.call_args_list,
            [
                call(
                    user_id="me",
                    max_results=2,
                    page_token=None,
                    query=None,
                    label_ids=None,
                    include_spam_trash=False,
                ),
                call(
                    user_id="me",
                    max_results=1,
                    page_token="page-2",
                    query=None,
                    label_ids=None,
                    include_spam_trash=False,
                ),
            ],
        )

    def test_total_limit_stops_pagination(
        self,
    ) -> None:
        self.list_page.return_value = {
            "messages": [
                {
                    "id": "message-1",
                    "threadId": "thread-1",
                }
            ],
            "nextPageToken": "unused-page",
        }

        result = self.client.list_message_references(
            total_limit=1,
            page_size=500,
        )

        self.assertEqual(len(result), 1)
        self.list_page.assert_called_once()
        self.assertEqual(
            self.list_page.call_args.kwargs[
                "max_results"
            ],
            1,
        )

    def test_invalid_limits_are_rejected(
        self,
    ) -> None:
        for invalid_limit in (
            0,
            10_001,
            True,
            1.5,
        ):
            with self.subTest(
                total_limit=invalid_limit
            ):
                with self.assertRaises(
                    GmailClientError
                ):
                    self.client.list_message_references(
                        total_limit=invalid_limit
                    )

        for invalid_page_size in (
            0,
            501,
            True,
            1.5,
        ):
            with self.subTest(
                page_size=invalid_page_size
            ):
                with self.assertRaises(
                    GmailClientError
                ):
                    self.client.list_message_references(
                        page_size=invalid_page_size
                    )

        self.list_page.assert_not_called()

    def test_invalid_responses_are_rejected(
        self,
    ) -> None:
        invalid_responses = (
            None,
            {"messages": {}},
            {"messages": [None]},
            {
                "messages": [
                    {"threadId": "thread-1"}
                ]
            },
            {
                "messages": [
                    {"id": "message-1"}
                ]
            },
            {
                "messages": [
                    {
                        "id": " ",
                        "threadId": "thread-1",
                    }
                ]
            },
        )

        for response in invalid_responses:
            with self.subTest(response=response):
                self.list_page.reset_mock()
                self.list_page.side_effect = None
                self.list_page.return_value = response

                with self.assertRaises(
                    GmailClientError
                ):
                    self.client.list_message_references()

    def test_invalid_pagination_is_rejected(
        self,
    ) -> None:
        invalid_token_responses = (
            {
                "messages": [
                    {
                        "id": "message-1",
                        "threadId": "thread-1",
                    }
                ],
                "nextPageToken": "",
            },
            {
                "messages": [],
                "nextPageToken": "page-2",
            },
        )

        for response in invalid_token_responses:
            with self.subTest(response=response):
                self.list_page.reset_mock()
                self.list_page.side_effect = None
                self.list_page.return_value = response

                with self.assertRaises(
                    GmailClientError
                ):
                    self.client.list_message_references()

        self.list_page.reset_mock()
        self.list_page.side_effect = [
            {
                "messages": [
                    {
                        "id": "message-1",
                        "threadId": "thread-1",
                    }
                ],
                "nextPageToken": "repeated-page",
            },
            {
                "messages": [
                    {
                        "id": "message-2",
                        "threadId": "thread-2",
                    }
                ],
                "nextPageToken": "repeated-page",
            },
        ]

        with self.assertRaises(GmailClientError):
            self.client.list_message_references()

    def test_client_error_is_sanitized(
        self,
    ) -> None:
        self.list_page.side_effect = (
            MessagesClientError(
                "sensitive-lower-level-value"
            )
        )

        with self.assertRaises(
            GmailClientError
        ) as raised:
            self.client.list_message_references()

        self.assertEqual(
            str(raised.exception),
            "Failed to list Gmail messages",
        )
        self.client.logger.error.assert_called_once_with(
            "Gmail message pagination failed"
        )

        logged_value = (
            self.client
            .logger
            .error
            .call_args
            .args[0]
        )
        self.assertNotIn(
            "sensitive-lower-level-value",
            logged_value,
        )


class GmailClientMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = GmailClient.__new__(
            GmailClient
        )
        self.client.messages = Mock()
        self.client.logger = Mock()

        self.get_message = (
            self.client
            .messages
            .get_message
        )

    def test_valid_metadata_returns_typed_message(
        self,
    ) -> None:
        self.get_message.return_value = {
            "id": " message-1 ",
            "threadId": " thread-1 ",
            "internalDate": " 1757721600123 ",
            "labelIds": [
                " INBOX ",
                "CATEGORY_UPDATES",
            ],
            "payload": {
                "headers": [
                    {
                        "name": " subject ",
                        "value": " Weekly update ",
                    },
                    {
                        "name": "FROM",
                        "value": (
                            "Publisher "
                            "<news@example.com>"
                        ),
                    },
                    {
                        "name": "Date",
                        "value": (
                            "Sat, 13 Sep 2026 "
                            "09:00:00 +0200"
                        ),
                    },
                    {
                        "name": "List-Id",
                        "value": (
                            "<weekly.example.com>"
                        ),
                    },
                    {
                        "name": "List-Unsubscribe",
                        "value": (
                            "<mailto:unsubscribe@example.com>"
                        ),
                    },
                    {
                        "name": "Precedence",
                        "value": "bulk",
                    },
                    {
                        "name": "Auto-Submitted",
                        "value": "auto-generated",
                    },
                ]
            },
        }

        result = self.client.get_message_metadata(
            message_id="message-1",
            user_id="me",
        )

        self.assertEqual(
            result,
            GmailMessageMetadata(
                message_id="message-1",
                thread_id="thread-1",
                internal_date_ms=1757721600123,
                label_ids=(
                    "INBOX",
                    "CATEGORY_UPDATES",
                ),
                subject="Weekly update",
                sender=(
                    "Publisher <news@example.com>"
                ),
                date_header=(
                    "Sat, 13 Sep 2026 "
                    "09:00:00 +0200"
                ),
                list_id="<weekly.example.com>",
                list_unsubscribe=(
                    "<mailto:unsubscribe@example.com>"
                ),
                precedence="bulk",
                auto_submitted="auto-generated",
            ),
        )

        self.get_message.assert_called_once_with(
            message_id="message-1",
            user_id="me",
            message_format="metadata",
            metadata_headers=(
                GmailClient.MESSAGE_METADATA_HEADERS
            ),
        )

    def test_missing_optional_metadata_returns_none(
        self,
    ) -> None:
        self.get_message.return_value = {
            "id": "message-1",
            "threadId": "thread-1",
            "internalDate": "1757721600123",
            "payload": {
                "headers": [],
            },
        }

        result = self.client.get_message_metadata(
            "message-1"
        )

        self.assertEqual(result.label_ids, ())
        self.assertIsNone(result.subject)
        self.assertIsNone(result.sender)
        self.assertIsNone(result.date_header)
        self.assertIsNone(result.list_id)
        self.assertIsNone(
            result.list_unsubscribe
        )
        self.assertIsNone(result.precedence)
        self.assertIsNone(
            result.auto_submitted
        )

    def test_invalid_metadata_responses_are_rejected(
        self,
    ) -> None:
        valid_base = {
            "id": "message-1",
            "threadId": "thread-1",
            "internalDate": "1757721600123",
            "labelIds": ["INBOX"],
            "payload": {
                "headers": [],
            },
        }

        invalid_responses = (
            None,
            {},
            {
                **valid_base,
                "id": " ",
            },
            {
                **valid_base,
                "threadId": "",
            },
            {
                **valid_base,
                "internalDate": None,
            },
            {
                **valid_base,
                "internalDate": 1757721600123,
            },
            {
                **valid_base,
                "internalDate": "not-a-number",
            },
            {
                **valid_base,
                "labelIds": {},
            },
            {
                **valid_base,
                "labelIds": ["INBOX", " "],
            },
            {
                **valid_base,
                "payload": None,
            },
            {
                **valid_base,
                "payload": {
                    "headers": {},
                },
            },
            {
                **valid_base,
                "payload": {
                    "headers": [None],
                },
            },
            {
                **valid_base,
                "payload": {
                    "headers": [
                        {
                            "name": "",
                            "value": "value",
                        }
                    ],
                },
            },
            {
                **valid_base,
                "payload": {
                    "headers": [
                        {
                            "name": "From",
                            "value": None,
                        }
                    ],
                },
            },
        )

        for response in invalid_responses:
            with self.subTest(response=response):
                self.get_message.reset_mock()
                self.get_message.side_effect = None
                self.get_message.return_value = (
                    response
                )

                with self.assertRaises(
                    GmailClientError
                ):
                    self.client.get_message_metadata(
                        "message-1"
                    )

    def test_lower_client_error_is_sanitized(
        self,
    ) -> None:
        self.get_message.side_effect = (
            MessagesClientError(
                "sensitive-lower-level-value"
            )
        )

        with self.assertRaises(
            GmailClientError
        ) as raised:
            self.client.get_message_metadata(
                "message-1"
            )

        self.assertEqual(
            str(raised.exception),
            (
                "Failed to retrieve "
                "Gmail message metadata"
            ),
        )

        logged_value = (
            self.client
            .logger
            .error
            .call_args
            .args[0]
        )

        self.assertNotIn(
            "sensitive-lower-level-value",
            logged_value,
        )

    def test_unexpected_error_is_sanitized(
        self,
    ) -> None:
        self.get_message.side_effect = RuntimeError(
            "sensitive-runtime-value"
        )

        with self.assertRaises(
            GmailClientError
        ) as raised:
            self.client.get_message_metadata(
                "message-1"
            )

        self.assertEqual(
            str(raised.exception),
            (
                "Failed to retrieve "
                "Gmail message metadata"
            ),
        )

        logged_value = (
            self.client
            .logger
            .error
            .call_args
            .args[0]
        )

        self.assertIn(
            "RuntimeError",
            logged_value,
        )
        self.assertNotIn(
            "sensitive-runtime-value",
            logged_value,
        )

if __name__ == "__main__":
    unittest.main()
