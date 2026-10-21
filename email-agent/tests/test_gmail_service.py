import time

import gmail_service as gs


def test_state_roundtrip():
    assert gs.verify_state(gs.make_state("secret"), "secret")


def test_state_rejects_wrong_secret_and_tampering():
    s = gs.make_state("secret")
    assert not gs.verify_state(s, "other")
    tampered = s[:-1] + ("1" if s[-1] == "0" else "0")  # always a different last character
    assert not gs.verify_state(tampered, "secret")
    assert not gs.verify_state("garbage", "secret")


def test_state_expires():
    payload = f"abc.{int(time.time()) - 5000}"
    assert not gs.verify_state(f"{payload}.{gs._sign(payload, 'secret')}", "secret")


def test_mailto_is_encoded():
    link = gs.mailto_link("a@b.com", "Hi there", "Line1\nLine2 & more")
    assert link.startswith("mailto:a@b.com?subject=Hi%20there&body=")
    assert "%0A" in link and "%26" in link


def test_build_message_with_cc_bcc_and_attachment():
    msg = gs.build_message(
        "a@b.com", "Hi", "Body",
        cc=["c@d.com"], bcc=["e@f.com"],
        attachments=[("resume.pdf", b"%PDF-1.4 test", "application/pdf")],
    )
    assert msg["To"] == "a@b.com" and msg["Cc"] == "c@d.com" and msg["Bcc"] == "e@f.com"
    attachments = list(msg.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "resume.pdf"
    assert attachments[0].get_content_type() == "application/pdf"


def test_build_message_without_extras_has_no_cc_bcc():
    msg = gs.build_message("a@b.com", "Hi", "Body")
    assert msg["Cc"] is None and msg["Bcc"] is None


def test_compose_links_include_cc_and_bcc():
    gmail = gs.gmail_compose_link("a@b.com", "Hi there", "Line1\nLine2", ["c@d.com"], ["e@f.com"])
    assert gmail.startswith("https://mail.google.com/mail/?view=cm")
    assert "cc=c%40d.com" in gmail and "bcc=e%40f.com" in gmail and "su=Hi%20there" in gmail
    assert "https://outlook.office.com/mail/deeplink/compose?" in gs.outlook_compose_link("a@b.com", "s", "b")
    assert "cc=c%40d.com" in gs.mailto_link("a@b.com", "s", "b", ["c@d.com"])