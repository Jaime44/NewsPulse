from __future__ import annotations

import unittest

from httplib2 import Response
from unittest.mock import Mock

from googleapiclient.errors import HttpError

from app.tools.gmail.messages_client import (
    MessagesClient,
    MessagesClientError,
)


class MessagesClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Mock()
        self.messages_api = (
            self.service
            .users
            .return_value
            .messages
            .return_value
        )
        self.request = (
            self.messages_api
            .list
            .return_value
        )
        self.get_request = (
            self.messages_api
            .get
            .return_value
        )
        self.client = MessagesClient(self.service)
        self.client.logger = Mock()

    def test_list_messages_uses_safe_defaults(
        self,
    ) -> None:
        response = {
            "messages": [],
        }
        self.request.execute.return_value = response

        result = self.client.list_messages()

        self.assertEqual(result, response)
        self.messages_api.list.assert_called_once_with(
            userId="me",
            maxResults=100,
            includeSpamTrash=False,
        )

    def test_list_messages_forwards_all_filters(
        self,
    ) -> None:
        response = {
            "messages": [
                {
                    "id": "message-1",
                    "threadId": "thread-1",
                }
            ],
            "nextPageToken": "following-page",
        }
        self.request.execute.return_value = response

        result = self.client.list_messages(
            user_id=" me ",
            max_results=500,
            page_token=" next-page ",
            query=" after:1757721600 ",
            label_ids=(
                " INBOX ",
                " newsletter-label ",
            ),
            include_spam_trash=True,
        )

        self.assertEqual(result, response)
        self.messages_api.list.assert_called_once_with(
            userId="me",
            maxResults=500,
            includeSpamTrash=True,
            pageToken="next-page",
            q="after:1757721600",
            labelIds=[
                "INBOX",
                "newsletter-label",
            ],
        )

    def test_invalid_max_results_is_rejected(
        self,
    ) -> None:
        invalid_values = (
            0,
            501,
            True,
            1.5,
        )

        for invalid_value in invalid_values:
            with self.subTest(value=invalid_value):
                with self.assertRaises(
                    MessagesClientError
                ):
                    self.client.list_messages(
                        max_results=invalid_value
                    )

        self.messages_api.list.assert_not_called()

    def test_invalid_request_values_are_rejected(
        self,
    ) -> None:
        invalid_arguments = (
            {"user_id": " "},
            {"page_token": " "},
            {"query": ""},
            {"label_ids": []},
            {"label_ids": ["INBOX", " "]},
            {"include_spam_trash": "false"},
        )

        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                with self.assertRaises(
                    MessagesClientError
                ):
                    self.client.list_messages(
                        **arguments
                    )

        self.messages_api.list.assert_not_called()

    def test_http_error_is_sanitized(
        self,
    ) -> None:
        http_error = HttpError(
            Response({"status": "429"}),
            (
                b'{"error":{"message":'
                b'"sensitive-provider-message"}}'
            ),
        )
        self.request.execute.side_effect = http_error

        with self.assertRaises(
            MessagesClientError
        ) as raised:
            self.client.list_messages()

        self.assertEqual(
            str(raised.exception),
            "Failed to list Gmail messages",
        )

        log_message = (
            self.client
            .logger
            .error
            .call_args
            .args[0]
        )

        self.assertIn("429", log_message)
        self.assertNotIn(
            "sensitive-provider-message",
            log_message,
        )

    def test_unexpected_error_is_sanitized(
        self,
    ) -> None:
        self.request.execute.side_effect = RuntimeError(
            "sensitive-runtime-value"
        )

        with self.assertRaises(
            MessagesClientError
        ) as raised:
            self.client.list_messages()

        self.assertEqual(
            str(raised.exception),
            "Failed to list Gmail messages",
        )

        log_message = (
            self.client
            .logger
            .error
            .call_args
            .args[0]
        )

        self.assertIn("RuntimeError", log_message)
        self.assertNotIn(
            "sensitive-runtime-value",
            log_message,
        )

    def test_get_message_uses_metadata_defaults(
        self,
    ) -> None:
        response = {
            "id": "message-1",
            "threadId": "thread-1",
        }
        self.get_request.execute.return_value = response

        result = self.client.get_message(
            " message-1 "
        )

        self.assertEqual(result, response)
        self.messages_api.get.assert_called_once_with(
            userId="me",
            id="message-1",
            format="metadata",
        )

    def test_get_message_forwards_metadata_headers(
        self,
    ) -> None:
        response = {
            "id": "message-1",
        }
        self.get_request.execute.return_value = response

        result = self.client.get_message(
            message_id=" message-1 ",
            user_id=" user@example.com ",
            message_format=" METADATA ",
            metadata_headers=(
                " From ",
                " Subject ",
            ),
        )

        self.assertEqual(result, response)
        self.messages_api.get.assert_called_once_with(
            userId="user@example.com",
            id="message-1",
            format="metadata",
            metadataHeaders=[
                "From",
                "Subject",
            ],
        )

    def test_get_message_rejects_invalid_values(
        self,
    ) -> None:
        invalid_arguments = (
            {"message_id": ""},
            {"message_id": None},
            {
                "message_id": "message-1",
                "user_id": " ",
            },
            {
                "message_id": "message-1",
                "message_format": "invalid",
            },
            {
                "message_id": "message-1",
                "message_format": None,
            },
            {
                "message_id": "message-1",
                "metadata_headers": "From",
            },
            {
                "message_id": "message-1",
                "metadata_headers": [],
            },
            {
                "message_id": "message-1",
                "metadata_headers": ["From", " "],
            },
            {
                "message_id": "message-1",
                "message_format": "full",
                "metadata_headers": ["From"],
            },
        )

        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                with self.assertRaises(
                    MessagesClientError
                ):
                    self.client.get_message(
                        **arguments
                    )

        self.messages_api.get.assert_not_called()

    def test_get_message_http_error_is_sanitized(
        self,
    ) -> None:
        http_error = HttpError(
            Response({"status": "404"}),
            (
                b'{"error":{"message":'
                b'"sensitive-provider-message"}}'
            ),
        )
        self.get_request.execute.side_effect = http_error

        with self.assertRaises(
            MessagesClientError
        ) as raised:
            self.client.get_message("message-1")

        self.assertEqual(
            str(raised.exception),
            "Failed to retrieve Gmail message",
        )

        log_message = (
            self.client
            .logger
            .error
            .call_args
            .args[0]
        )

        self.assertIn("404", log_message)
        self.assertNotIn(
            "sensitive-provider-message",
            log_message,
        )

    def test_get_message_unexpected_error_is_sanitized(
        self,
    ) -> None:
        self.get_request.execute.side_effect = RuntimeError(
            "sensitive-runtime-value"
        )

        with self.assertRaises(
            MessagesClientError
        ) as raised:
            self.client.get_message("message-1")

        self.assertEqual(
            str(raised.exception),
            "Failed to retrieve Gmail message",
        )

        log_message = (
            self.client
            .logger
            .error
            .call_args
            .args[0]
        )

        self.assertIn("RuntimeError", log_message)
        self.assertNotIn(
            "sensitive-runtime-value",
            log_message,
        )

if __name__ == "__main__":
    unittest.main()
