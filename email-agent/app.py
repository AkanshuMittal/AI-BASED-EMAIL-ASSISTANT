import html
import os
import uuid

import streamlit as st

st.set_page_config(page_title="AI email assistant", page_icon="✉️", layout="centered")

for _name in ("GROQ_API_KEY", "LANGSMITH_API_KEY", "LANGSMITH_TRACING", "LANGSMITH_PROJECT"):
    try:
        _val = str(st.secrets.get(_name, "")).strip()
        if _val:
            os.environ[_name] = _val
    except Exception:
        pass

from langgraph.types import Command

import gmail_service as gs  
from graph import (  
    EMAIL_RE, MAX_BODY_CHARS, MAX_FEEDBACK_CHARS, MAX_QUESTION_CHARS, MAX_REVISIONS,
    PLACEHOLDER_RE, SENDABLE_RE, build_graph, parse_addresses, suggest_email,
)

MAX_LLM_CALLS = 12  # per browser session, protects the shared Groq key
MAX_ATTACH_MB = 5
ATTACH_TYPES = ["pdf", "doc", "docx", "txt", "png", "jpg", "jpeg"]
TONES = ["Professional", "Friendly", "Formal", "Concise", "Apologetic"]
EXAMPLES = [
    ("Reschedule a meeting", "Write to Priya (priya@example.com) asking to move our Thursday meeting to Friday."),
    ("Request leave", "Email hr@example.com requesting leave from 4 to 6 November for a family event."),
    ("Thank-you note", "Tell sam@example.com thanks for the interview and that I'm still very interested."),
]

st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;6..72,600&display=swap');
:root{--accent:#3FB8C4}
.block-container{max-width:780px;padding-top:4.5rem;padding-bottom:3rem}
footer{visibility:hidden}
header[data-testid="stHeader"]{background:transparent}

/* type scale: brand 3rem > section 1.6rem > body 1rem > labels .9rem > captions .8rem */
.brand{font-family:'Newsreader',Georgia,serif!important;font-size:3rem!important;font-weight:600!important;line-height:1.1!important;margin:0 0 .3rem!important}
.tagline{font-size:1.05rem!important;opacity:.7!important;margin:0 0 1.6rem!important}
.section-title{font-family:'Newsreader',Georgia,serif!important;font-size:1.6rem!important;font-weight:600!important;line-height:1.2!important;margin:.2rem 0 .25rem!important}
.section-sub{font-size:.9rem!important;opacity:.65!important;margin:0 0 .9rem!important}
.side-title{font-size:1.1rem!important;font-weight:600!important;margin:0 0 .6rem!important}
[data-testid="stWidgetLabel"] p{font-size:.9rem;font-weight:600}
[data-testid="stCaptionContainer"]{font-size:.8rem}

.stages{display:flex;gap:1.6rem;margin:0 0 1.8rem;border-bottom:1px solid rgba(128,128,128,.3)}
.stage{padding:.4rem 0;opacity:.45;font-size:.95rem;border-bottom:3px solid transparent;margin-bottom:-1px}
.stage.on{opacity:1;font-weight:600;border-bottom-color:var(--accent)}
.stage.past{opacity:.8}

/* the paper letter: stays light in dark mode so the draft is the focal point */
.letter{background:#FBFAF7;color:#1d1d1b;border-radius:6px;padding:1.6rem 1.9rem;
  box-shadow:0 1px 2px rgba(0,0,0,.3),0 14px 34px rgba(0,0,0,.45);margin:.5rem 0 1.2rem;border-left:5px solid var(--accent)}
.letter .meta{font-size:.85rem;color:#5b5b55;line-height:1.7;border-bottom:1px solid #e4e1d8;padding-bottom:.7rem;margin-bottom:1rem}
.letter .meta b{color:#1d1d1b}
.letter .text{font-family:'Newsreader',Georgia,serif;font-size:1.1rem;line-height:1.65;max-width:62ch}

.gbtn{display:block;text-align:center;padding:.55rem .9rem;border-radius:8px;border:1px solid rgba(128,128,128,.5);
  text-decoration:none!important;font-weight:600;font-size:.95rem;color:inherit!important}
.gbtn:hover{border-color:var(--accent);color:var(--accent)!important}
</style>
""",
    unsafe_allow_html=True,
)

def title(text, sub=""):
    st.markdown(f'<div class="section-title">{html.escape(text)}</div>', unsafe_allow_html=True)
    if sub:
        st.markdown(f'<div class="section-sub">{html.escape(sub)}</div>', unsafe_allow_html=True)


# ---------- setup ----------
@st.cache_resource
def get_graph():
    return build_graph()  # InMemorySaver shared across sessions; thread_id isolates users


def get_oauth_cfg():
    keys = ("client_id", "client_secret", "redirect_uri")
    cfg = {
        "client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
        "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
        "redirect_uri": os.getenv("GOOGLE_REDIRECT_URI", ""),
    }
    if not all(cfg.values()):  # fall back to .streamlit/secrets.toml
        try:
            cfg = dict(st.secrets["google_oauth"])
        except Exception:
            return None
    return cfg if all(cfg.get(k) for k in keys) else None


graph = get_graph()
OAUTH = get_oauth_cfg()

for k, v in {
    "thread_id": None, "phase": "compose", "draft": None, "version": 0, "llm_calls": 0,
    "user": None, "outcome": None, "final": None, "error": "", "prompt": "",
    "show_cc": False, "show_bcc": False, "nonce": 0,
}.items():
    st.session_state.setdefault(k, v)

# ---------- OAuth callback (Google redirects back with ?code=...) ----------
qp = st.query_params
if OAUTH and "code" in qp:
    try:
        if not gs.verify_state(qp.get("state", ""), OAUTH["client_secret"]):
            raise ValueError("The sign-in request expired. Please try again.")
        st.session_state.user = gs.exchange_code(OAUTH, qp["code"])
    except Exception as e:
        st.session_state.error = f"Google sign-in failed: {e}"
    st.query_params.clear()
    st.rerun()
elif "error" in qp:
    st.session_state.error = "Google sign-in was cancelled."
    st.query_params.clear()
    st.rerun()


# ---------- helpers ----------
def cfg():
    # run_name/tags make the LangSmith traces easy to find (no personal data in them).
    return {
        "configurable": {"thread_id": st.session_state.thread_id},
        "run_name": "quill-email",
        "tags": ["quill"],
    }


def reset_flow():
    for k in ("thread_id", "draft", "outcome", "final"):
        st.session_state[k] = None
    st.session_state.phase = "compose"
    st.session_state.error = ""
    st.session_state.show_cc = st.session_state.show_bcc = False
    st.session_state.nonce += 1  # fresh Cc/Bcc/attachment widgets for the next email


def sync_from_graph():
    snap = graph.get_state(cfg())
    v = snap.values
    status = v.get("status")
    if status == "error":
        st.session_state.error = v.get("error", "Something went wrong.")
        st.session_state.phase = "compose"
    elif snap.next:  # paused at the review interrupt
        # LangGraph only stores fields that were written, so fall back to defaults.
        defaults = {"recipient_name": "", "recipient_email": "", "subject": "", "body": "", "revisions": 0}
        st.session_state.draft = {k: v.get(k, d) for k, d in defaults.items()}
        st.session_state.version += 1
        st.session_state.phase = "review"
    else:
        st.session_state.phase = "done"
        if status == "cancelled":
            st.session_state.outcome = {"kind": "cancelled", "msg": v.get("error", "")}


def run(payload, calls):
    st.session_state.llm_calls += calls
    st.session_state.error = ""
    try:
        graph.invoke(payload, config=cfg())
        sync_from_graph()
    except Exception as e:
        st.session_state.error = f"The AI service had a problem: {e}"


def try_send():
    f, user = st.session_state.final, st.session_state.user
    try:
        gs.send_gmail(user["token"], f["to"], f["subject"], f["body"],
                      cc=f["cc"], bcc=f["bcc"], attachments=f["attachments"])
        st.session_state.outcome = {"kind": "sent"}
    except Exception as e:
        hint = " Your Google session may have expired. Sign out and sign in again." if "401" in str(e) else ""
        st.session_state.outcome = {"kind": "failed", "msg": f"{e}{hint}"}


def letter(f):
    e = html.escape
    meta = f"<b>To</b> {e(f['to'])}<br>"
    if f.get("cc"):
        meta += f"<b>Cc</b> {e(', '.join(f['cc']))}<br>"
    if f.get("bcc"):
        meta += f"<b>Bcc</b> {e(', '.join(f['bcc']))}<br>"
    meta += f"<b>Subject</b> {e(f['subject'])}"
    if f.get("attachments"):
        meta += "<br><b>Attachments</b> " + e(", ".join(a[0] for a in f["attachments"]))
    text = e(f["body"]).replace("\n", "<br>")
    st.markdown(
        f'<div class="letter"><div class="meta">{meta}</div><div class="text">{text}</div></div>',
        unsafe_allow_html=True,
    )


def stages():
    order = ["compose", "review", "done"]
    labels = ["Describe", "Review", "Send"]
    cur = order.index(st.session_state.phase)
    items = "".join(
        f'<div class="stage {"on" if i == cur else "past" if i < cur else ""}">{l}</div>'
        for i, l in enumerate(labels)
    )
    st.markdown(f'<div class="stages">{items}</div>', unsafe_allow_html=True)


def set_prompt(text):
    st.session_state.prompt = text


def apply_fix(key, bad, good):
    """Runs when a suggestion button is clicked: replace the address in that input."""
    st.session_state[key] = st.session_state.get(key, "").replace(bad, good)



# ---------- sidebar ----------
with st.sidebar:
    st.markdown('<div class="side-title">Your account</div>', unsafe_allow_html=True)
    user = st.session_state.user
    if user:
        if user.get("picture"):
            st.image(user["picture"], width=56)
        st.markdown(f"**{user['name']}**  \n{user['email']}")
        st.caption("Approved emails are sent from this account. Quill can send mail but can't read your inbox.")
        if st.button("Sign out", use_container_width=True):
            gs.revoke_token(user["token"])
            st.session_state.user = None
            st.rerun()
    elif OAUTH:
        url = gs.auth_url(OAUTH, gs.make_state(OAUTH["client_secret"]))
        st.markdown(
            f'<a class="gbtn" href="{html.escape(url, quote=True)}" target="_self">Sign in with Google</a>',
            unsafe_allow_html=True,
        )
        st.caption("Optional. Without signing in, you get a draft to copy or open in your own mail app.")
    else:
        st.info("Google sign-in isn't set up here, so Quill runs in draft-only mode.")

    st.divider()
    used = st.session_state.llm_calls
    st.progress(min(used / MAX_LLM_CALLS, 1.0))
    st.caption(f"AI requests this session: {used} of {MAX_LLM_CALLS}")
    if st.button("Start over", use_container_width=True):
        reset_flow()
        st.rerun()

# ---------- header ----------
st.markdown('<div class="brand">AI - Email Assistant</div>', unsafe_allow_html=True)
st.markdown('<div class="tagline">Describe the email. Review the draft. Send it only when you approve.</div>', unsafe_allow_html=True)
stages()

if st.session_state.error:
    st.error(st.session_state.error)

limit_hit = st.session_state.llm_calls >= MAX_LLM_CALLS
if limit_hit:
    st.warning("You've reached the AI request limit for this session. Refresh the page to start a new one.")

phase = st.session_state.phase

# ---------- 1. compose ----------
if phase == "compose":
    title("Write your email", "Say who it's for (with their email address) and why you're writing.")
    st.text_area(
        "Describe your email", key="prompt", height=130, label_visibility="collapsed",
        max_chars=MAX_QUESTION_CHARS,
        placeholder="Write to Rahul (rahul@example.com) asking for Friday off because of a family event.",
    )
    cols = st.columns(len(EXAMPLES))
    for i, (col, (label, text)) in enumerate(zip(cols, EXAMPLES)):
        col.button(label, key=f"ex{i}", on_click=set_prompt, args=(text,), use_container_width=True)
    tone = st.radio("Tone", TONES, horizontal=True)
    default_name = st.session_state.user["name"] if st.session_state.user else ""
    sender = st.text_input("Your name (used in the sign-off)", value=default_name,
                           key="sender_name", max_chars=60, placeholder="e.g. Akanshu Mittal")

    if st.button("Draft email", type="primary", use_container_width=True,
                 disabled=limit_hit or not st.session_state.prompt.strip()):
        st.session_state.thread_id = uuid.uuid4().hex
        with st.spinner("Writing your draft…"):
            run({"question": st.session_state.prompt.strip(), "tone": tone.lower(),
                 "sender_name": sender.strip()}, calls=2)
        st.rerun()

# ---------- 2. review ----------
elif phase == "review":
    title("Review your draft", "Edit anything you like. Nothing is sent until you approve.")
    d, ver, nonce = st.session_state.draft, st.session_state.version, st.session_state.nonce
    signed_in = st.session_state.user is not None

    with st.container(border=True):
        # To row with Cc / Bcc toggles, Gmail style
        c0, c1, c2, c3 = st.columns([0.7, 8, 1, 1.3], vertical_alignment="center")
        c0.markdown("**To**")
        st.session_state.setdefault(f"to_{ver}", d["recipient_email"])
        to = c1.text_input("To", key=f"to_{ver}", label_visibility="collapsed")
        if c2.button("Cc", key="tgl_cc", type="primary" if st.session_state.show_cc else "secondary",
                     use_container_width=True):
            st.session_state.show_cc = not st.session_state.show_cc
            st.rerun()
        if c3.button("Bcc", key="tgl_bcc", type="primary" if st.session_state.show_bcc else "secondary",
                     use_container_width=True):
            st.session_state.show_bcc = not st.session_state.show_bcc
            st.rerun()

        cc = bcc = ""
        if st.session_state.show_cc:
            r0, r1 = st.columns([0.7, 10.3], vertical_alignment="center")
            r0.markdown("**Cc**")
            cc = r1.text_input("Cc", key=f"cc_{nonce}", label_visibility="collapsed",
                               placeholder="name@example.com, another@example.com")
        if st.session_state.show_bcc:
            r0, r1 = st.columns([0.7, 10.3], vertical_alignment="center")
            r0.markdown("**Bcc**")
            bcc = r1.text_input("Bcc", key=f"bcc_{nonce}", label_visibility="collapsed",
                                placeholder="hidden recipients, separated by commas")

        subj = st.text_input("Subject", value=d["subject"], key=f"subj_{ver}", max_chars=200)
        body = st.text_area("Body", value=d["body"], height=300, key=f"body_{ver}", max_chars=MAX_BODY_CHARS)

        files = []
        if signed_in:
            files = st.file_uploader("Attachments", type=ATTACH_TYPES, accept_multiple_files=True,
                                     key=f"files_{nonce}",
                                     help="For example your resume. PDF, Word, text or images.") or []
            st.caption(f"Up to {MAX_ATTACH_MB} MB in total.")
        else:
            st.caption("Sign in with Google (sidebar) to attach files such as your resume.")

    cc_ok, cc_bad = parse_addresses(cc)
    bcc_ok, bcc_bad = parse_addresses(bcc)
    too_big = sum(f.size for f in files) > MAX_ATTACH_MB * 1024 * 1024
    if cc_bad:
        st.caption(f"Check these Cc addresses: {', '.join(cc_bad)}")
    if bcc_bad:
        st.caption(f"Check these Bcc addresses: {', '.join(bcc_bad)}")
    if too_big:
        st.error(f"Attachments are over {MAX_ATTACH_MB} MB. Remove or compress a file.")
    if PLACEHOLDER_RE.search(subj + " " + body):
        st.warning("The draft still has a placeholder like [Recipient Name]. Edit it before approving.")

    unusual = [a for a in [to.strip(), *cc_ok, *bcc_ok] if a and SENDABLE_RE.match(a) and not EMAIL_RE.match(a)]
    if unusual:
        st.warning(f"These addresses look unusual: {', '.join(unusual)}. They will be used exactly as typed. "
                   "If one is wrong, a delivery failure notice will arrive in your Gmail inbox.")
        fields = [(f"to_{ver}", to.strip())] + [(f"cc_{nonce}", a) for a in cc_ok] + [(f"bcc_{nonce}", a) for a in bcc_ok]
        suggestions = [(key, addr, suggest_email(addr)) for key, addr in fields if suggest_email(addr)]
        for i, (key, addr, good) in enumerate(suggestions):
            st.button(f"Use {good} instead of {addr}", key=f"fix_{i}", on_click=apply_fix, args=(key, addr, good))
        if suggestions:
            st.caption("Only use a suggestion if it is the right address. Nothing changes unless you click.")
    valid = bool(SENDABLE_RE.match(to.strip()) and subj.strip() and body.strip()
                 and not cc_bad and not bcc_bad and not too_big)
    st.caption(f"Revisions used: {d['revisions']} of {MAX_REVISIONS}.")

    b1, b2 = st.columns([3, 1])
    approve_label = "Approve and send" if signed_in else "Approve"
    if b1.button(approve_label, type="primary", use_container_width=True, disabled=not valid):
        graph.invoke(
            Command(resume={"action": "approve", "subject": subj.strip(), "body": body.strip(),
                            "recipient_email": to.strip()}),
            config=cfg(),
        )
        st.session_state.final = {
            "to": to.strip(), "cc": cc_ok, "bcc": bcc_ok,
            "subject": subj.strip(), "body": body.strip(),
            # files stay in this browser session only, never in graph state or checkpoints
            "attachments": [(f.name, f.getvalue(), f.type or "") for f in files],
        }
        st.session_state.phase = "done"
        if signed_in:
            with st.spinner("Sending…"):
                try_send()
        else:
            st.session_state.outcome = {"kind": "draft"}
        st.rerun()
    if b2.button("Cancel", use_container_width=True):
        graph.invoke(Command(resume={"action": "cancel"}), config=cfg())
        st.session_state.outcome = {"kind": "cancelled", "msg": ""}
        st.session_state.final = None
        st.session_state.phase = "done"
        st.rerun()
    if not valid:
        st.caption("Enter a valid recipient, subject and body to continue.")

    out_of_revisions = d["revisions"] >= MAX_REVISIONS
    with st.expander("Ask for changes", expanded=False):
        fb = st.text_input("What should change?", key=f"fb_{ver}", max_chars=MAX_FEEDBACK_CHARS,
                           placeholder="Make it shorter. Or: sign off as Akanshu Mittal.",
                           disabled=out_of_revisions)
        if out_of_revisions:
            st.caption("You've used all revisions. Edit the text above, or start over.")
        if st.button("Revise draft", disabled=out_of_revisions or limit_hit or not fb.strip()):
            with st.spinner("Revising…"):
                # send the current text so manual edits are kept when the AI revises
                run(Command(resume={"action": "revise", "feedback": fb.strip(), "subject": subj.strip(),
                                    "body": body.strip(), "recipient_email": to.strip()}), calls=1)
            st.rerun()

# ---------- 3. done ----------
else:
    out, f = st.session_state.outcome or {"kind": "cancelled", "msg": ""}, st.session_state.final
    kind = out["kind"]
    title({"sent": "Email sent", "failed": "Email not sent", "draft": "Your draft is ready"}.get(kind, "Cancelled"))

    if kind == "sent":
        st.success(f"Email sent from {st.session_state.user['email']}.")
        letter(f)
        st.caption("If an address was wrong, a delivery failure notice will arrive in your Gmail inbox.")
    elif kind == "failed":
        st.error(f"The email wasn't sent. {out['msg']}")
        letter(f)
        if st.session_state.user and st.button("Try sending again", type="primary"):
            with st.spinner("Sending…"):
                try_send()
            st.rerun()
    elif kind == "draft":
        st.info("Nothing was sent, because you're not signed in. Open the draft in your mail service or copy it.")
        letter(f)
        args = (f["to"], f["subject"], f["body"], f["cc"], f["bcc"])
        l1, l2, l3 = st.columns(3)
        l1.link_button("Open in Gmail", gs.gmail_compose_link(*args), type="primary", use_container_width=True)
        l2.link_button("Open in Outlook", gs.outlook_compose_link(*args), use_container_width=True)
        l3.link_button("Default mail app", gs.mailto_link(*args), use_container_width=True)
        st.caption("Gmail and Outlook open in your browser. 'Default mail app' only works if your computer "
                   "has a mail app set for mailto links. To attach files, sign in with Google.")
        st.caption("Or copy the text (use the copy icon):")
        st.code(f["body"], language=None, wrap_lines=True)
    else:
        st.info(out.get("msg") or "Cancelled. Nothing was sent.")

    if st.button("Write another email", use_container_width=(kind != "failed")):
        reset_flow()
        st.rerun()