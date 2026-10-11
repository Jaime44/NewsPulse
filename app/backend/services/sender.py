"""Reserve the outbound-email boundary for the future delivery pipeline."""


def send_email(to_address: str, subject: str, html_content: str) -> None:
    """Leave delivery unimplemented for the supplied email content.

    Args:
        to_address: Intended recipient address.
        subject: Intended message subject.
        html_content: Intended HTML message body.

    Returns:
        ``None``; this function is currently a placeholder.
    """
    # TODO: Integrate SMTP or the Gmail API.
    pass
