from graph import EMAIL_RE, MAX_REVISIONS, EmailState, router


def test_email_regex():
    assert EMAIL_RE.match("a@b.com")
    assert not EMAIL_RE.match("not-an-email")
    assert not EMAIL_RE.match("a b@c.com")


def test_router_done_when_approved_or_cancelled():
    assert router(EmailState(status="approved")) == "done"
    assert router(EmailState(status="cancelled")) == "done"


def test_router_redrafts_within_limit():
    assert router(EmailState(status="drafting", revisions=MAX_REVISIONS)) == "draft"


def test_router_cancels_past_limit():
    assert router(EmailState(status="drafting", revisions=MAX_REVISIONS + 1)) == "cancel"


def test_parse_addresses():
    from graph import parse_addresses

    ok, bad = parse_addresses("a@b.com, c@d.org; nope, e@f.io")
    assert ok == ["a@b.com", "c@d.org", "e@f.io"]
    assert bad == ["nope"]
    assert parse_addresses("") == ([], [])


def test_normalize_body_restores_line_breaks():
    from graph import normalize_body

    flat = "Dear Priya, I need to move our meeting to Friday. Thank you. Sincerely, Akanshu"
    out = normalize_body(flat)
    assert out.startswith("Dear Priya,\n\n")
    assert out.endswith("Sincerely,\nAkanshu")


def test_normalize_body_keeps_good_formatting():
    from graph import normalize_body

    good = "Dear Priya,\n\nSee you Friday.\n\nBest regards,\nAkanshu"
    assert normalize_body(good) == good


def test_normalize_body_fixes_escaped_newlines():
    from graph import normalize_body

    assert "\n" in normalize_body("Hi Sam,\\n\\nThanks.\\n\\nBest regards,\\nA")


def test_placeholder_detection():
    from graph import PLACEHOLDER_RE

    assert PLACEHOLDER_RE.search("Dear [Recipient Name],")
    assert not PLACEHOLDER_RE.search("Dear Priya,")


def test_clip_limits_length():
    from graph import clip

    assert clip("x" * 5000, 1000) == "x" * 1000
    assert clip("short", 1000) == "short"
    assert clip(None, 10) == ""


def test_first_draft_without_name_says_no_name():
    from graph import EmailState, build_draft_prompt

    prompt = build_draft_prompt(EmailState(mail_reason="leave", recipient_name="Priya"))
    assert "no name" in prompt


def test_revision_lets_feedback_add_a_name():
    from graph import EmailState, build_draft_prompt

    state = EmailState(subject="s", body="Hi,\n\nBest regards,", feedback="add my name Akanshu")
    prompt = build_draft_prompt(state)
    assert "no name" not in prompt           # the old rule must not fight the feedback
    assert "follow the feedback" in prompt
    assert "add my name Akanshu" in prompt


def test_revision_knows_sender_name():
    from graph import EmailState, build_draft_prompt

    state = EmailState(subject="s", body="b", feedback="add my name", sender_name="Akanshu Mittal")
    prompt = build_draft_prompt(state)
    assert "Sender name: Akanshu Mittal" in prompt


def test_email_regex_is_strict_about_domain_ending():
    assert EMAIL_RE.match("hr@example.com")
    assert EMAIL_RE.match("first.last+tag@mail.example.co.uk")
    assert not EMAIL_RE.match("hr@example.com2345")   # digits glued to the TLD
    assert not EMAIL_RE.match("hr@example")
    assert not EMAIL_RE.match("hr@@example.com")


def test_find_emails_returns_typos_unchanged():
    from graph import find_emails

    assert find_emails("Write to Rahul (rahul@example.com), please.") == ["rahul@example.com"]
    assert find_emails("Email hr@example.com2345 about leave") == ["hr@example.com2345"]
    assert find_emails("a@b.com and A@B.com and c@d.org") == ["a@b.com", "c@d.org"]
    assert find_emails("no address here") == []



def test_sendable_check_is_loose_like_gmail():
    from graph import SENDABLE_RE

    assert SENDABLE_RE.match("hr@example.com2345")      # unusual, but sent exactly as typed
    assert SENDABLE_RE.match("a@b.co.uk")
    assert not SENDABLE_RE.match("hr@example")          # no domain ending
    assert not SENDABLE_RE.match("hr@@example.com")
    assert not SENDABLE_RE.match("a b@c.com")
    assert not SENDABLE_RE.match("nope")


def test_parse_addresses_keeps_unusual_ones_unchanged():
    from graph import parse_addresses

    assert parse_addresses("sam@rrtexample.com23445, bad") == (["sam@rrtexample.com23445"], ["bad"])


def test_suggest_email_only_strips_junk_after_the_domain():
    from graph import suggest_email

    assert suggest_email("sam@rrtexample.com23445") == "sam@rrtexample.com"
    assert suggest_email("hr@example.com2345") == "hr@example.com"
    assert suggest_email("hr@example.com_") == "hr@example.com"
    assert suggest_email("hr@example.com") is None        # already fine
    assert suggest_email("hr@example") is None            # nothing safe to suggest
    assert suggest_email("hr@@example.com") is None


def test_suggest_email_never_changes_letters():
    from graph import suggest_email

    assert suggest_email("sam@gmial.com99") == "sam@gmial.com"   # domain typo is NOT corrected


# ---- the recipient address must never be changed by the LLM ----

def test_retriever_keeps_the_typed_address(monkeypatch):
    import graph

    def fake_llm(schema, prompt, attempts=3):
        # even if the model "wants" to tidy the address, UserDetails has no email field
        return graph.UserDetails(mail_reason="leave", recipient_name="Sam")

    monkeypatch.setattr(graph, "structured_invoke", fake_llm)
    out = graph.retriever_node(graph.EmailState(question="Email sam@rrtexample.com23445 asking for leave"))
    assert out["status"] == "drafting"
    assert out["recipient_email"] == "sam@rrtexample.com23445"


def test_retriever_skips_llm_for_unusable_address(monkeypatch):
    import graph

    def boom(*args, **kwargs):
        raise AssertionError("the LLM must not be called for an unusable address")

    monkeypatch.setattr(graph, "structured_invoke", boom)
    out = graph.retriever_node(graph.EmailState(question="Email hr@example asking for leave"))
    assert out["status"] == "error"


def test_retriever_rejects_two_addresses(monkeypatch):
    import graph

    monkeypatch.setattr(graph, "structured_invoke", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    out = graph.retriever_node(graph.EmailState(question="Email a@x.com and b@y.com about the meeting"))
    assert out["status"] == "error"
    assert "more than one" in out["error"]


def test_draft_prompt_forbids_addresses_in_text():
    from graph import EmailState, build_draft_prompt

    assert "Do not write any email addresses" in build_draft_prompt(EmailState(mail_reason="leave"))