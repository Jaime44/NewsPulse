from __future__ import annotations

import re

from collections.abc import Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import (
    urlsplit,
    urlunsplit,
)

from app.backend.services.message_content import (
    DecodedTextPart,
)


class ContentParsingError(RuntimeError):
    """Raised when decoded message content cannot be parsed."""


@dataclass(frozen=True, slots=True)
class ExtractedMessageContent:
    """Readable message text and validated HTTP links."""

    text: str
    links: tuple[str, ...]
    source_mime_type: str


URL_PATTERN = re.compile(
    r"""https?://[^\s<>"']+""",
    re.IGNORECASE,
)

TRAILING_URL_PUNCTUATION = (
    ".,;:!?)]}"
)


def _normalize_text(
    value: str,
) -> str:
    """Normalize whitespace while preserving paragraphs."""

    normalized_newlines = (
        value
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )

    lines: list[str] = []

    for raw_line in normalized_newlines.split(
        "\n"
    ):
        normalized_line = " ".join(
            raw_line.split()
        )

        if normalized_line:
            lines.append(
                normalized_line
            )

    return "\n".join(lines)


def _normalize_http_url(
    value: object,
    *,
    max_url_chars: int,
) -> str | None:
    """Validate and normalize one absolute HTTP URL."""

    if not isinstance(value, str):
        return None

    candidate = (
        value
        .strip()
        .rstrip(
            TRAILING_URL_PUNCTUATION
        )
    )

    if (
        not candidate
        or len(candidate) > max_url_chars
        or any(
            character.isspace()
            for character in candidate
        )
    ):
        return None

    try:
        parsed = urlsplit(
            candidate
        )

        if (
            parsed.scheme.casefold()
            not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.hostname is None
        ):
            return None

        port = parsed.port
    except ValueError:
        return None

    normalized_hostname = (
        parsed.hostname.casefold()
    )

    if ":" in normalized_hostname:
        normalized_netloc = (
            f"[{normalized_hostname}]"
        )
    else:
        normalized_netloc = (
            normalized_hostname
        )

    if port is not None:
        normalized_netloc += (
            f":{port}"
        )

    return urlunsplit(
        (
            parsed.scheme.casefold(),
            normalized_netloc,
            parsed.path,
            parsed.query,
            "",
        )
    )


def _extract_urls_from_text(
    text: str,
    *,
    max_url_chars: int,
) -> list[str]:
    """Extract absolute HTTP URLs from readable text."""

    links: list[str] = []

    for match in URL_PATTERN.finditer(
        text
    ):
        normalized_url = (
            _normalize_http_url(
                match.group(0),
                max_url_chars=(
                    max_url_chars
                ),
            )
        )

        if normalized_url is not None:
            links.append(
                normalized_url
            )

    return links


class _ReadableHtmlParser(HTMLParser):
    """Collect readable HTML text and anchor targets."""

    IGNORED_TAGS = frozenset(
        {
            "script",
            "style",
            "noscript",
            "template",
            "svg",
        }
    )

    BLOCK_TAGS = frozenset(
        {
            "address",
            "article",
            "aside",
            "blockquote",
            "br",
            "div",
            "footer",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "header",
            "li",
            "main",
            "nav",
            "ol",
            "p",
            "section",
            "table",
            "td",
            "th",
            "tr",
            "ul",
        }
    )

    def __init__(self) -> None:
        super().__init__(
            convert_charrefs=True
        )

        self.text_chunks: list[str] = []
        self.raw_links: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[
            tuple[str, str | None]
        ],
    ) -> None:
        normalized_tag = tag.casefold()

        if (
            normalized_tag
            in self.IGNORED_TAGS
        ):
            self.ignored_depth += 1
            return

        if self.ignored_depth:
            return

        if normalized_tag in self.BLOCK_TAGS:
            self.text_chunks.append(
                "\n"
            )

        if normalized_tag != "a":
            return

        for name, value in attrs:
            if (
                name.casefold() == "href"
                and value is not None
            ):
                self.raw_links.append(
                    value
                )
                break

    def handle_endtag(
        self,
        tag: str,
    ) -> None:
        normalized_tag = tag.casefold()

        if (
            normalized_tag
            in self.IGNORED_TAGS
        ):
            if self.ignored_depth:
                self.ignored_depth -= 1
            return

        if self.ignored_depth:
            return

        if normalized_tag in self.BLOCK_TAGS:
            self.text_chunks.append(
                "\n"
            )

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[
            tuple[str, str | None]
        ],
    ) -> None:
        self.handle_starttag(
            tag,
            attrs,
        )
        self.handle_endtag(
            tag
        )

    def handle_data(
        self,
        data: str,
    ) -> None:
        if not self.ignored_depth:
            self.text_chunks.append(
                data
            )


def _parse_html(
    html_content: str,
    *,
    max_url_chars: int,
) -> tuple[str, list[str]]:
    """Convert HTML to readable text and validated links."""

    parser = _ReadableHtmlParser()

    try:
        parser.feed(
            html_content
        )
        parser.close()
    except Exception as exc:
        raise ContentParsingError(
            "HTML content could not be parsed"
        ) from exc

    readable_text = _normalize_text(
        "".join(
            parser.text_chunks
        )
    )

    links: list[str] = []

    for raw_link in parser.raw_links:
        normalized_url = (
            _normalize_http_url(
                raw_link,
                max_url_chars=(
                    max_url_chars
                ),
            )
        )

        if normalized_url is not None:
            links.append(
                normalized_url
            )

    links.extend(
        _extract_urls_from_text(
            readable_text,
            max_url_chars=(
                max_url_chars
            ),
        )
    )

    return readable_text, links


def _deduplicate_links(
    links: Sequence[str],
    *,
    max_links: int,
) -> tuple[str, ...]:
    """Deduplicate links while preserving their order."""

    unique_links: list[str] = []
    seen_links: set[str] = set()

    for link in links:
        if link in seen_links:
            continue

        seen_links.add(
            link
        )
        unique_links.append(
            link
        )

        if len(unique_links) >= max_links:
            break

    return tuple(unique_links)


class MessageContentParser:
    """Select readable message text and extract safe links."""

    SUPPORTED_MIME_TYPES = frozenset(
        {
            "text/plain",
            "text/html",
        }
    )

    DEFAULT_MAX_TEXT_CHARS = 1_000_000
    DEFAULT_MAX_LINKS = 200
    DEFAULT_MAX_URL_CHARS = 2_048

    def __init__(
        self,
        *,
        max_text_chars: int = (
            DEFAULT_MAX_TEXT_CHARS
        ),
        max_links: int = (
            DEFAULT_MAX_LINKS
        ),
        max_url_chars: int = (
            DEFAULT_MAX_URL_CHARS
        ),
    ) -> None:
        limits = {
            "max_text_chars": (
                max_text_chars
            ),
            "max_links": max_links,
            "max_url_chars": (
                max_url_chars
            ),
        }

        for name, value in limits.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
            ):
                raise ValueError(
                    f"{name} must be "
                    "a positive integer"
                )

        self.max_text_chars = (
            max_text_chars
        )
        self.max_links = max_links
        self.max_url_chars = (
            max_url_chars
        )

    def parse(
        self,
        parts: Sequence[
            DecodedTextPart
        ],
    ) -> ExtractedMessageContent:
        """Select text and links from decoded MIME parts."""

        if (
            isinstance(parts, (str, bytes))
            or not isinstance(parts, Sequence)
        ):
            raise ContentParsingError(
                "Decoded text parts are invalid"
            )

        plain_candidates: list[str] = []
        html_candidates: list[str] = []
        collected_links: list[str] = []

        for part in parts:
            if not isinstance(
                part,
                DecodedTextPart,
            ):
                raise ContentParsingError(
                    "Decoded text parts are invalid"
                )

            if (
                part.mime_type
                not in self.SUPPORTED_MIME_TYPES
            ):
                raise ContentParsingError(
                    "Unsupported decoded MIME type"
                )

            if not isinstance(part.text, str):
                raise ContentParsingError(
                    "Decoded text is invalid"
                )

            if part.mime_type == "text/plain":
                normalized_text = (
                    _normalize_text(
                        part.text
                    )
                )

                if normalized_text:
                    plain_candidates.append(
                        normalized_text
                    )

                collected_links.extend(
                    _extract_urls_from_text(
                        part.text,
                        max_url_chars=(
                            self.max_url_chars
                        ),
                    )
                )

            else:
                (
                    readable_html,
                    html_links,
                ) = _parse_html(
                    part.text,
                    max_url_chars=(
                        self.max_url_chars
                    ),
                )

                if readable_html:
                    html_candidates.append(
                        readable_html
                    )

                collected_links.extend(
                    html_links
                )

        if plain_candidates:
            selected_text = max(
                plain_candidates,
                key=len,
            )
            source_mime_type = "text/plain"
        elif html_candidates:
            selected_text = max(
                html_candidates,
                key=len,
            )
            source_mime_type = "text/html"
        else:
            raise ContentParsingError(
                "No readable message content found"
            )

        if (
            len(selected_text)
            > self.max_text_chars
        ):
            raise ContentParsingError(
                "Readable message content exceeds "
                "the configured text limit"
            )

        return ExtractedMessageContent(
            text=selected_text,
            links=_deduplicate_links(
                collected_links,
                max_links=self.max_links,
            ),
            source_mime_type=(
                source_mime_type
            ),
        )
