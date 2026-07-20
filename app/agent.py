"""The Claude agent: triages a ticket and drafts/decides a reply.

One agentic loop per ticket. The model can research via three tools
(KB search, local docs, past tickets) and must finish by calling
`submit_result` with a structured verdict.
"""
from __future__ import annotations

import json
import logging

import anthropic

from .config import settings
from .freshdesk import fd, strip_html
from .knowledge import search_docs
from . import training

log = logging.getLogger("agent")

client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

TOOLS = [
    {
        "name": "search_knowledge_base",
        "description": "Search the company's Freshdesk knowledge base (Solutions articles) by keyword. Use this to ground answers in official documentation.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Keywords to search for"}},
            "required": ["query"],
        },
    },
    {
        "name": "search_local_docs",
        "description": "Search internal reference docs (policies, product docs, FAQs) provided by the support team.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "search_past_tickets",
        "description": "Search previously resolved tickets for how similar issues were handled. Returns subjects and snippets.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Keywords describing the issue"}},
            "required": ["query"],
        },
    },
    {
        "name": "submit_result",
        "description": "Submit your final triage and reply decision. Call this exactly once, when done researching.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "One short kebab-case category, e.g. how-to, billing-question, bug-report, refund-request, account, order-status, complaint, feature-request, spam, other",
                },
                "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"]},
                "sentiment": {"type": "string", "enum": ["positive", "neutral", "frustrated", "angry"]},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "1-4 short tags"},
                "summary": {"type": "string", "description": "1-2 sentence summary of the customer's issue"},
                "reply": {
                    "type": "string",
                    "description": "The full customer-facing reply, plain text with paragraphs. Empty string if no reply should be sent (e.g. spam).",
                },
                "confidence": {
                    "type": "integer",
                    "description": "0-100: how confident you are that the reply fully and correctly resolves the ticket without human review. Be conservative: anything involving refunds, account changes, legal issues, or missing information should score below 70.",
                },
                "needs_human": {
                    "type": "boolean",
                    "description": "True if a human must handle this (angry customer, refund/credit decision, legal/security issue, ambiguous request, or you could not find a grounded answer).",
                },
                "reasoning": {"type": "string", "description": "Brief internal note for the support team on why you chose this handling."},
            },
            "required": ["category", "priority", "sentiment", "tags", "summary", "reply", "confidence", "needs_human", "reasoning"],
        },
    },
]

SYSTEM = """You are a customer support agent for {company}. You are given a support ticket
(which may originate from email or the website chat widget) and must:

1. Research the issue using your tools. Always check the knowledge base and local docs
   before answering; check past tickets when the issue is unusual.
2. Triage it: category, priority, sentiment, tags, summary.
3. Write the best possible reply, grounded ONLY in what you found in the tools or the
   ticket itself. Never invent product facts, prices, policies, or commitments.

Reply style:
- Warm, concise, professional. Address the customer by first name if known.
- Chat-source tickets get shorter, more conversational replies; email gets fuller ones.
- If you cannot resolve the issue, write a holding reply that asks the right clarifying
  question or tells the customer a specialist will follow up — and set needs_human=true.
- Never promise refunds, credits, or exceptions; flag those for a human.
- If the customer asks for a human, a live agent, a phone call, or to speak with
  someone: set needs_human=true, category "human-requested", priority at least
  medium, and keep the reply to a one-line acknowledgement that a team member
  will follow up shortly (include the support line (801) 852-9898 if they want
  to call). Do NOT attempt to answer their underlying question.
- Sign off as "{signature}".

Finish by calling submit_result exactly once."""


def _run_tool(name: str, args: dict) -> str:
    try:
        if name == "search_knowledge_base":
            articles = fd.search_solutions(args["query"])
            if not articles:
                return "No KB articles found."
            out = []
            for a in articles[:4]:
                out.append(f"### {a.get('title')}\n{strip_html(a.get('description'))[:3000]}")
            return "\n\n".join(out)
        if name == "search_local_docs":
            return search_docs(args["query"])
        if name == "search_past_tickets":
            results = fd.search_tickets(args["query"])
            if not results:
                return "No similar past tickets found."
            out = []
            for t in results[:5]:
                out.append(
                    f"- #{t['id']} [{t.get('status')}] {t.get('subject')}: "
                    f"{strip_html(t.get('description_text') or t.get('description'))[:400]}"
                )
            return "\n".join(out)
    except Exception as e:  # tool errors go back to the model, not up the stack
        log.exception("Tool %s failed", name)
        return f"Tool error: {e}"
    return f"Unknown tool {name}"


CHAT_SYSTEM = """You are a live-chat support copilot for {company}. You are given the
transcript of an IN-PROGRESS chat between a customer and a human support agent.
Your job: suggest the next reply the HUMAN AGENT should send.

- Research with your tools first (knowledge base, internal docs) — ground every fact.
- Keep it SHORT and conversational: 1-3 sentences, chat tone, no email sign-offs.
- If the human agent has already said something, match their tone and don't repeat it.
- If the customer asks for a human, they already have one — just help the agent answer.
- Never promise refunds, credits, or exceptions; if the right move is a judgment call,
  say so in the reasoning and set needs_human=true.
- Finish by calling submit_result exactly once; put the suggested reply in "reply"
  (plain text, no signature)."""


def handle_chat(transcript: str) -> dict:
    """Run the agent on a live chat transcript. Returns the submit_result payload."""
    messages = [{"role": "user", "content": f"Live chat so far:\n\n{transcript}\n\nSuggest the agent's next reply."}]
    system = CHAT_SYSTEM.format(company=settings.company_name) + training.corrections_block()

    for _ in range(8):
        resp = client.messages.create(
            model=settings.model,
            max_tokens=settings.max_tokens,
            system=system,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            messages.append({"role": "user", "content": "Please call submit_result with your final suggestion."})
            continue
        results = []
        for tu in tool_uses:
            if tu.name == "submit_result":
                return dict(tu.input)
            results.append(
                {"type": "tool_result", "tool_use_id": tu.id, "content": _run_tool(tu.name, dict(tu.input))}
            )
        messages.append({"role": "user", "content": results})
    raise RuntimeError("Chat agent did not submit a result within the iteration limit")


def handle_ticket(ticket: dict) -> dict:
    """Run the agent loop on a ticket dict (with conversations). Returns the
    structured submit_result payload."""
    ticket_text = fd.ticket_to_text(ticket)
    messages = [{"role": "user", "content": f"Handle this support ticket:\n\n{ticket_text}"}]
    system = SYSTEM.format(company=settings.company_name, signature=settings.agent_signature) + training.corrections_block()

    for _ in range(12):  # hard cap on loop iterations
        resp = client.messages.create(
            model=settings.model,
            max_tokens=settings.max_tokens,
            system=system,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            # Model answered in prose without submitting — nudge it once.
            messages.append({"role": "user", "content": "Please call submit_result with your final decision."})
            continue

        results = []
        for tu in tool_uses:
            if tu.name == "submit_result":
                log.info("Ticket #%s verdict: %s", ticket["id"], json.dumps(tu.input)[:500])
                return dict(tu.input)
            results.append(
                {"type": "tool_result", "tool_use_id": tu.id, "content": _run_tool(tu.name, dict(tu.input))}
            )
        messages.append({"role": "user", "content": results})

    raise RuntimeError("Agent did not submit a result within the iteration limit")
