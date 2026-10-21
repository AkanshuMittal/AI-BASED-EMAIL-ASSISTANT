import os
import re
import sqlite3
from functools import lru_cache
from typing import Literal

from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field

load_dotenv()

MAX_REVISIONS = 3
MAX_QUESTION_CHARS = 1000
MAX_FEEDBACK_CHARS = 300
MAX_BODY_CHARS = 3000
MAX_OUTPUT_TOKENS = 1500  # generous: reasoning models spend tokens thinking before the answer
CHECKPOINT_DB = os.getenv("CHECKPOINT_DB", "checkpoints.db")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}$")
_EMAIL_CANDIDATE_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+")
SENDABLE_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
PLACEHOLDER_RE = re.compile(r"\[[^\]\n]{2,40}\]")  # e.g. [Recipient Name]
SIGNOFF_RE = r"(Best regards|Kind regards|Warm regards|Regards|Sincerely|Yours sincerely)"


def find_emails(text: str) -> list:
    """Find address-like strings in text with code, never with the LLM.

    Returns them exactly as typed (minus trailing punctuation), de-duplicated.
    A typo like 'a@b.com2345' is returned exactly as typed: the address is never
    corrected or changed by the app or the LLM.
    """
    found = [m.rstrip(".-") for m in _EMAIL_CANDIDATE_RE.findall(text or "")]
    unique, seen = [], set()
    for addr in found:
        if addr.lower() not in seen:
            seen.add(addr.lower())
            unique.append(addr)
    return unique


def suggest_email(addr: str):
    """Suggest a fix for an unusual address, or None.

    Only one very safe fix is offered: junk glued after the domain ending
    (e.g. 'a@b.com2345' -> 'a@b.com'). Letters are never added, removed or swapped,
    so a different domain is never guessed. The user must click to accept it.
    """
    if EMAIL_RE.match(addr):
        return None
    fixed = re.sub(r"[^A-Za-z]+$", "", addr)
    return fixed if fixed != addr and EMAIL_RE.match(fixed) else None


def clip(text: str, limit: int) -> str:
    return (text or "")[:limit]


def parse_addresses(text: str):
    """Split 'a@x.com, b@y.com' into (valid, invalid) lists."""
    parts = [p.strip() for p in re.split(r"[,;]", text or "") if p.strip()]
    return [p for p in parts if SENDABLE_RE.match(p)], [p for p in parts if not SENDABLE_RE.match(p)]


def normalize_body(text: str) -> str:
    """Make sure the body has real line breaks even if the model flattened it."""
    text = text.replace("\\n", "\n").replace("\r\n", "\n").strip()
    if "\n" not in text:
        text = re.sub(r"^((?:Dear|Hi|Hello|Hey)\b[^,\n]{0,60},)\s+", r"\1\n\n", text)
        text = re.sub(r"\s+" + SIGNOFF_RE + r",\s*", r"\n\n\1,\n", text)
    return text


@lru_cache(maxsize=1)
def get_llm() -> ChatGroq:
    return ChatGroq(model="openai/gpt-oss-20b", temperature=0.4, max_tokens=MAX_OUTPUT_TOKENS)


def structured_invoke(schema, prompt, attempts: int = 3):
    """Call the LLM for structured output, retrying across methods.

    Small models sometimes fail to call the tool ("tool_use_failed"), so we try
    Groq's json_schema mode first and fall back to function calling.
    """
    methods = ["json_schema", "function_calling", "json_schema"]
    last_error = None
    for method in methods[:attempts]:
        try:
            return get_llm().with_structured_output(schema, method=method).invoke(prompt)
        except Exception as e:
            last_error = e
    raise last_error


class EmailState(BaseModel):
    question: str = ""
    tone: str = "professional"
    sender_name: str = ""
    mail_reason: str = ""
    recipient_name: str = ""
    recipient_email: str = ""
    subject: str = ""
    body: str = ""
    feedback: str = ""
    revisions: int = 0
    status: Literal["drafting", "awaiting_review", "approved", "cancelled", "error"] = "drafting"
    error: str = ""


class UserDetails(BaseModel):
    mail_reason: str = Field(description="Why the user wants to send this email.")
    recipient_name: str = Field(description="Recipient name, or '' if not given.")


class DraftEmail(BaseModel):
    subject: str = Field(description="Email subject, at most 10 words.")
    body: str = Field(
        description=(
            "Full email body as plain text with real line breaks: greeting on its own line, "
            "short paragraphs separated by a blank line, then the sign-off and name on separate lines."
        )
    )


def retriever_node(state: EmailState) -> dict:
    question = clip(state.question, MAX_QUESTION_CHARS)

    # The recipient address is found by code and never rewritten by the LLM.
    addresses = find_emails(question)
    if not addresses:
        return {"status": "error", "error": "Add the recipient's email address, like name@example.com."}
    bad = [a for a in addresses if not SENDABLE_RE.match(a)]
    if bad:
        return {
            "status": "error",
            "error": f"'{bad[0]}' isn't a usable email address. It needs a name, an @ and a domain like example.com.",
        }
    if len(addresses) > 1:
        return {
            "status": "error",
            "error": "I found more than one email address. Write only the main recipient here "
                     "and add Cc in the review screen.",
        }
    email = addresses[0]

    try:
        details = structured_invoke(
            UserDetails,
            f"The recipient's email is {email}. From this request, extract the reason for the email "
            f"and the recipient's name (empty if not given): {question}",
        )
    except Exception as e:  # LLM / parsing failure
        return {"status": "error", "error": f"Could not read your request ({e}). Try rephrasing."}

    if not details.mail_reason.strip():
        return {"status": "error", "error": "Say what the email is about."}

    return {
        "recipient_name": details.recipient_name.strip(),
        "recipient_email": email,
        "mail_reason": details.mail_reason.strip(),
        "status": "drafting",
    }


def after_retriever(state: EmailState) -> Literal["draft", "end"]:
    return "end" if state.status == "error" else "draft"


def build_draft_prompt(state: EmailState) -> str:
    base_rules = (
        "Use only the facts given. Do not invent reasons, dates, appointments or promises. "
        "Never use placeholders like [Your Name] or [Recipient Name]. "
        "Do not write any email addresses in the subject or body. "
        "Format the body with real line breaks: the greeting on its own line, short paragraphs "
        "separated by a blank line, then the sign-off on separate lines. "
        "Max 200 words. Return only subject and body."
    )
    greeting_name = state.recipient_name or "the recipient"

    if state.feedback:
        # The user's feedback must win over default rules (e.g. "add my name after Best regards").
        if state.sender_name:
            signoff = (
                f"Sign-off: 'Best regards,' then the sender's name {state.sender_name} on the next line, "
                "unless the feedback says otherwise."
            )
        else:
            signoff = (
                "Sign-off: keep the existing one unless the feedback asks to change it. "
                "If the feedback gives a name to add, put it on the line after 'Best regards,'."
            )
        return (
            f"Revise this email using the feedback. Keep the tone {state.tone}. "
            "If the feedback conflicts with a rule below, follow the feedback.\n"
            f"Rules: {base_rules} {signoff}\n\n"
            f"Sender name: {state.sender_name or 'not provided'}\n"
            f"Recipient: {greeting_name}\nSubject: {clip(state.subject, 200)}\n"
            f"Body:\n{clip(state.body, MAX_BODY_CHARS)}\n\n"
            f"Feedback: {clip(state.feedback, MAX_FEEDBACK_CHARS)}"
        )

    signoff = (
        f"Sign off with 'Best regards,' on its own line, then the name {state.sender_name} on the next line."
        if state.sender_name
        else "Sign off with just 'Best regards,' and no name."
    )
    return (
        f"Write a {state.tone} email.\nTo: {greeting_name}\n"
        f"Purpose: {state.mail_reason}\n{base_rules} {signoff}"
    )


def draft_node(state: EmailState) -> dict:
    prompt = build_draft_prompt(state)
    try:
        draft = structured_invoke(DraftEmail, prompt)
    except Exception as e:
        return {"status": "error", "error": f"Drafting failed ({e}). Try again."}

    return {
        "subject": draft.subject.strip(),
        "body": normalize_body(draft.body),
        "feedback": "",
        "status": "awaiting_review",
    }


def after_draft(state: EmailState) -> Literal["review", "end"]:
    return "end" if state.status == "error" else "review"


def review_node(state: EmailState) -> dict:
    # Pauses the graph. The UI resumes it with a dict describing the user's decision.
    decision = interrupt(
        {"subject": state.subject, "body": state.body, "revisions": state.revisions}
    )
    action = decision.get("action")

    if action == "approve":
        return {
            "subject": decision.get("subject", state.subject),
            "body": decision.get("body", state.body),
            "recipient_email": decision.get("recipient_email", state.recipient_email),
            "status": "approved",
        }
    if action == "revise":
        feedback = decision.get("feedback", "").strip() or "Improve the email."
        return {
            "feedback": feedback,
            "subject": decision.get("subject", state.subject),
            "body": decision.get("body", state.body),
            "recipient_email": decision.get("recipient_email", state.recipient_email),
            "revisions": state.revisions + 1,
            "status": "drafting",
        }
    return {"status": "cancelled"}


def router(state: EmailState) -> Literal["draft", "cancel", "done"]:
    if state.status in ("approved", "cancelled"):
        return "done"
    if state.revisions > MAX_REVISIONS:
        return "cancel"
    return "draft"


def cancel_node(state: EmailState) -> dict:
    return {"status": "cancelled", "error": f"Stopped after {MAX_REVISIONS} revisions."}


def get_checkpointer():
    """SQLite checkpointer (state survives app restarts locally).

    Falls back to in-memory if langgraph-checkpoint-sqlite isn't installed.
    Note: Streamlit Cloud's disk resets on reboot, so persistence there is best-effort.
    """
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver

        conn = sqlite3.connect(CHECKPOINT_DB, check_same_thread=False)
        return SqliteSaver(conn)
    except Exception:
        return InMemorySaver()


def build_graph(checkpointer=None):
    g = StateGraph(EmailState)
    g.add_node("retriever", retriever_node)
    g.add_node("draft", draft_node)
    g.add_node("review", review_node)
    g.add_node("cancel", cancel_node)

    g.add_edge(START, "retriever")
    g.add_conditional_edges("retriever", after_retriever, {"draft": "draft", "end": END})
    g.add_conditional_edges("draft", after_draft, {"review": "review", "end": END})
    g.add_conditional_edges("review", router, {"draft": "draft", "cancel": "cancel", "done": END})
    g.add_edge("cancel", END)
    return g.compile(checkpointer=checkpointer or get_checkpointer())