"""Configuration loaded from environment variables."""
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _csv(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


@dataclass
class Settings:
    # Freshdesk
    freshdesk_domain: str = os.getenv("FRESHDESK_DOMAIN", "")  # e.g. "acme" for acme.freshdesk.com
    freshdesk_api_key: str = os.getenv("FRESHDESK_API_KEY", "")

    # Anthropic
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    model: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")
    max_tokens: int = int(os.getenv("MAX_TOKENS", "4096"))

    # Webhook security: Freshdesk automation sends this in the X-Webhook-Secret header
    webhook_secret: str = os.getenv("WEBHOOK_SECRET", "")

    # Behavior
    # Master switch: when false, the agent NEVER sends customer-facing replies;
    # everything is posted as a private note (draft) for human review.
    auto_reply_enabled: bool = _bool("AUTO_REPLY_ENABLED", False)
    # Only these categories may be auto-replied (agent-classified). Empty = none.
    auto_reply_categories: list[str] = field(
        default_factory=lambda: _csv("AUTO_REPLY_CATEGORIES", "how-to,account,billing-question,order-status")
    )
    # Minimum confidence (0-100) the agent must report to auto-send.
    auto_reply_min_confidence: int = int(os.getenv("AUTO_REPLY_MIN_CONFIDENCE", "85"))
    # Never auto-reply more than once per ticket.
    max_auto_replies_per_ticket: int = 1

    # Triage
    triage_enabled: bool = _bool("TRIAGE_ENABLED", True)
    # Optional JSON mapping of category -> Freshdesk group_id, e.g. '{"billing-question": 123}'
    group_routing_json: str = os.getenv("GROUP_ROUTING_JSON", "{}")

    # Company context injected into every prompt
    company_name: str = os.getenv("COMPANY_NAME", "our company")
    agent_signature: str = os.getenv("AGENT_SIGNATURE", "Support Team")
    knowledge_dir: str = os.getenv("KNOWLEDGE_DIR", "knowledge")

    def validate(self) -> list[str]:
        problems = []
        if not self.freshdesk_domain:
            problems.append("FRESHDESK_DOMAIN is not set")
        if not self.freshdesk_api_key:
            problems.append("FRESHDESK_API_KEY is not set")
        if not self.anthropic_api_key:
            problems.append("ANTHROPIC_API_KEY is not set")
        if not self.webhook_secret:
            problems.append("WEBHOOK_SECRET is not set (required to authenticate webhooks)")
        return problems


settings = Settings()
