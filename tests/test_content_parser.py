from __future__ import annotations

import unittest

from app.backend.services.content_parser import (
    ContentParsingError,
    ExtractedMessageContent,
    MessageContentParser,
)
from app.backend.services.message_content import (
    DecodedTextPart,
)


class MessageContentParserTests(
    unittest.TestCase
):
    def setUp(self) -> None:
        self.parser = (
            MessageContentParser()
        )

    def test_plain_text_is_preferred_and_links_are_deduplicated(
        self,
    ) -> None:
        parts = (
            DecodedTextPart(
                mime_type="text/plain",
                text=(
                    "Resumen semanal\n"
                    "Visita "
                    "https://Example.com/story."
                ),
            ),
            DecodedTextPart(
                mime_type="text/html",
                text=(
                    '<a href="'
                    'https://example.com/story#top'
                    '">Leer</a>'
                    '<a href="mailto:x@example.com">'
                    "Correo"
                    "</a>"
                ),
            ),
        )

        result = self.parser.parse(
            parts
        )

        self.assertEqual(
            result,
            ExtractedMessageContent(
                text=(
                    "Resumen semanal\n"
                    "Visita "
                    "https://Example.com/story."
                ),
                links=(
                    "https://example.com/story",
                ),
                source_mime_type="text/plain",
            ),
        )

    def test_html_is_used_when_plain_text_is_missing(
        self,
    ) -> None:
        html = (
            "<html>"
            "<head>"
            "<style>.hidden { color: red; }</style>"
            "</head>"
            "<body>"
            "<h1>Boletín &amp; noticias</h1>"
            "<p>Primer artículo</p>"
            "<script>alert('hidden')</script>"
            '<a href="'
            'https://Example.com/story?x=1#section'
            '">Leer más</a>'
            '<a href="/relative"></a>'
            '<a href="javascript:alert(1)"></a>'
            '<a href="'
            'https://user:pass@example.com/private'
            '"></a>'
            "</body>"
            "</html>"
        )

        result = self.parser.parse(
            (
                DecodedTextPart(
                    mime_type="text/html",
                    text=html,
                ),
            )
        )

        self.assertEqual(
            result,
            ExtractedMessageContent(
                text=(
                    "Boletín & noticias\n"
                    "Primer artículo\n"
                    "Leer más"
                ),
                links=(
                    (
                        "https://example.com/"
                        "story?x=1"
                    ),
                ),
                source_mime_type="text/html",
            ),
        )
        self.assertNotIn(
            "hidden",
            result.text,
        )
        self.assertNotIn(
            "alert",
            result.text,
        )

    def test_longest_plain_part_is_selected(
        self,
    ) -> None:
        result = self.parser.parse(
            (
                DecodedTextPart(
                    mime_type="text/plain",
                    text="Corto",
                ),
                DecodedTextPart(
                    mime_type="text/plain",
                    text=(
                        "  Artículo   más largo  "
                        "\r\n segunda línea "
                    ),
                ),
            )
        )

        self.assertEqual(
            result.text,
            (
                "Artículo más largo\n"
                "segunda línea"
            ),
        )
        self.assertEqual(
            result.links,
            (),
        )
        self.assertEqual(
            result.source_mime_type,
            "text/plain",
        )

    def test_plain_urls_are_normalized_in_order(
        self,
    ) -> None:
        text = (
            "Uno https://example.com/a). "
            "Dos HTTP://EXAMPLE.COM/b?x=1! "
            "Repite https://example.com/a"
        )

        result = self.parser.parse(
            (
                DecodedTextPart(
                    mime_type="text/plain",
                    text=text,
                ),
            )
        )

        self.assertEqual(
            result.links,
            (
                "https://example.com/a",
                "http://example.com/b?x=1",
            ),
        )

    def test_invalid_or_empty_parts_are_rejected(
        self,
    ) -> None:
        invalid_values = (
            "invalid",
            (),
            (
                object(),
            ),
            (
                DecodedTextPart(
                    mime_type="application/pdf",
                    text="content",
                ),
            ),
            (
                DecodedTextPart(
                    mime_type="text/plain",
                    text="   ",
                ),
            ),
        )

        for value in invalid_values:
            with self.subTest(
                value=value
            ):
                with self.assertRaises(
                    ContentParsingError
                ):
                    self.parser.parse(
                        value
                    )

    def test_text_limit_is_enforced(
        self,
    ) -> None:
        parser = MessageContentParser(
            max_text_chars=5,
        )

        with self.assertRaises(
            ContentParsingError
        ) as raised:
            parser.parse(
                (
                    DecodedTextPart(
                        mime_type="text/plain",
                        text="123456",
                    ),
                )
            )

        self.assertEqual(
            str(raised.exception),
            (
                "Readable message content exceeds "
                "the configured text limit"
            ),
        )

    def test_link_limit_is_enforced(
        self,
    ) -> None:
        parser = MessageContentParser(
            max_links=2,
        )

        result = parser.parse(
            (
                DecodedTextPart(
                    mime_type="text/plain",
                    text=(
                        "https://a.example/1 "
                        "https://b.example/2 "
                        "https://c.example/3"
                    ),
                ),
            )
        )

        self.assertEqual(
            result.links,
            (
                "https://a.example/1",
                "https://b.example/2",
            ),
        )

    def test_invalid_limits_are_rejected(
        self,
    ) -> None:
        invalid_limits = (
            {
                "max_text_chars": 0,
            },
            {
                "max_text_chars": True,
            },
            {
                "max_links": 0,
            },
            {
                "max_links": False,
            },
            {
                "max_url_chars": -1,
            },
            {
                "max_url_chars": True,
            },
        )

        for limits in invalid_limits:
            with self.subTest(
                limits=limits
            ):
                with self.assertRaises(
                    ValueError
                ):
                    MessageContentParser(
                        **limits
                    )


if __name__ == "__main__":
    unittest.main()