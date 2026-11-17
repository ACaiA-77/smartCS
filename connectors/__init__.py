"""External connectors for mail, GitHub and Feishu."""

from .base import (
    ConnectorConfigError,
    ConnectorError,
    canonical_payload_hash,
    http_request_json,
)
from .feishu import FeishuClient, verify_feishu_signature
from .github import GitHubClient
from .mail import GmailApiClient, ImapSmtpMailClient, MailProvider, MailRouter

__all__ = [
    "ConnectorConfigError",
    "ConnectorError",
    "FeishuClient",
    "GitHubClient",
    "GmailApiClient",
    "ImapSmtpMailClient",
    "MailProvider",
    "MailRouter",
    "canonical_payload_hash",
    "http_request_json",
    "verify_feishu_signature",
]
