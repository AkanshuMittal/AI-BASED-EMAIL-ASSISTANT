# Quill: AI Based Email Agent

[![CI](https://github.com/<username>/AI-BASED-EMAIL-ASSISTANT/actions/workflows/ci.yml/badge.svg)](https://github.com/<username>/AI-BASED-EMAIL-ASSISTANT/actions/workflows/ci.yml)
[![CD](https://github.com/<username>/AI-BASED-EMAIL-ASSISTANT/actions/workflows/cd.yml/badge.svg)](https://github.com/<username>/AI-BASED-EMAIL-ASSISTANT/actions/workflows/cd.yml)
![Python](https://img.shields.io/badge/python-3.11-blue)
![LangGraph](https://img.shields.io/badge/LangGraph-human--in--the--loop-1F6F78)
![Cloud Run](https://img.shields.io/badge/deployed%20on-Google%20Cloud%20Run-4285F4)

**Describe an email in plain language. The agent drafts it, you review and approve, and it is sent from your own Google account.**

**Live demo:** <LIVE_URL>
*Google sign-in is limited to approved test users while the app awaits Google's verification. Everyone else can still use the app in draft-only mode.*

| 1. Describe | 2. Review and edit | 3. Send |
|---|---|---|
| ![Describe the email](docs/screenshots/01-describe.png) | ![Review the draft](docs/screenshots/02-review.png) | ![Email sent](docs/screenshots/03-sent.png) |

## The problem

Writing routine professional emails (job applications, leave requests, follow-ups) is slow and repetitive. Simply wiring an LLM to an email account creates real risks:

- **Unreviewed output.** The model can invent facts or send something the user never approved.
- **Silent data changes.** LLMs "tidy" details. In testing, the model quietly changed a typo'd recipient address into a different one, which could send mail to the wrong person.
- **Unsafe sending.** Apps that send from a shared developer account let strangers send mail as the developer.

## The solution

Quill is an agent with a **human approval step** built into its workflow:

1. **Describe** the email in one sentence, including the recipient's address.
2. The agent **extracts the recipient and intent**, then **drafts** the email in the chosen tone.
3. The workflow **pauses for review**. The user edits the text, adds Cc/Bcc and attachments, or asks the AI for up to three revisions.
4. Only after **approval** is the email sent, from the **user's own Google account** (OAuth). Without signing in, the user gets a draft to open in Gmail/Outlook or copy.

## Key features

- Human-in-the-loop workflow: nothing is sent without explicit approval
- Editable draft with tone selection and AI revisions that keep the user's manual edits
- Cc / Bcc, and file attachments (for example a resume)
- Google OAuth sign-in with the narrowest scope (`gmail.send`: can send, cannot read the inbox)
- Draft-only fallback: "Open in Gmail", "Open in Outlook", default mail app, or copy
- Recipient address is read by code and never rewritten by the LLM; unusual addresses trigger a warning
- Cost and abuse guards for the public demo (per-session limit, input and output caps)
- Optional LangSmith tracing of every node, prompt and response

## Architecture

![System architecture](docs/architecture.png)

### LangGraph workflow

![LangGraph workflow](docs/graph.png)

| Node | What it does |
|---|---|
| `retriever` | Finds the recipient address with code (regex), then asks the LLM only for the reason and recipient name. Stops with a clear error if the address is missing, unusable or ambiguous. |
| `draft` | Writes the email, or revises it using the user's feedback and current edits. Structured output with retries. |
| `review` | Calls `interrupt()` and waits for the human: **approve**, **revise** or **cancel**. |
| `cancel` | Ends the run after the revision limit (3) is exceeded. |

State is saved by a **checkpointer** under a per-session `thread_id`, which is what lets the graph pause at `review` and resume later.

**Why sending is outside the graph.** OAuth tokens and attachments are handled in the UI layer after approval, so they never enter graph state or checkpoints.

## Tech stack

| Area | Tools |
|---|---|
| Agent / orchestration | LangGraph, LangChain, Pydantic |
| LLM | Groq (`openai/gpt-oss-20b`) |
| UI | Streamlit |
| Auth and email | Google OAuth 2.0, Gmail API |
| Persistence | SQLite checkpointer |
| Observability | LangSmith |
| Delivery | Docker, GitHub Actions, Google Cloud Run, Artifact Registry, Secret Manager |
| Testing | pytest |

## Engineering decisions

| Problem found | Decision |
|---|---|
| The LLM silently altered a recipient address | The address is extracted with regex and the LLM has no email field to return. Invalid format stops the run; unusual format shows a warning and an optional, click-to-accept suggestion (it only strips junk after the domain and never changes letters). |
| Small model sometimes fails structured output (`tool_use_failed`) | `structured_invoke` retries across `json_schema` and function-calling modes before surfacing an error. |
| Draft came back flattened or with `[Recipient Name]` placeholders | Explicit formatting rules in the prompt, a `normalize_body` safety net, and a placeholder warning in the UI. User feedback takes priority over default rules. |
| Streamlit loses session state on the OAuth redirect | The OAuth `state` is HMAC-signed and expires after 15 minutes, so it survives the redirect. Sign-out revokes the token. |
| Public demo could burn the shared LLM key | Per-session request limit, input length caps, `max_tokens`, scale-to-zero with a maximum of 2 instances, and a dedicated key. |
| Tokens and files must not be persisted | Sending and attachments live in the UI layer, outside checkpointed state. |

## Security and privacy

- No shared credentials: mail is sent from the signed-in user's own account
- Secrets live in Secret Manager or environment variables, never in git (`.env` and `secrets.toml` are ignored)
- GitHub deploys to Google Cloud with keyless Workload Identity Federation (no service account keys)
- LangSmith tracing is off in production because traces contain email text
- Attachments stay in the browser session and are limited in type and size (5 MB total)

## Project structure

```
.
├── app.py                    # Streamlit UI (compose, review, send)
├── graph.py                  # LangGraph workflow, prompts, address handling
├── gmail_service.py          # Google OAuth, Gmail send, Cc/Bcc, attachments, compose links
├── show_graph.py             # Generates docs/graph.png from the compiled graph
├── tests/
│   ├── test_graph.py         # routing, address handling, prompts, retriever guarantees
│   ├── test_gmail_service.py # OAuth state, message building, compose links
│   └── test_structured.py    # structured-output retry and fallback
├── deploy/
│   ├── setup_gcp.sh          # one-time Google Cloud setup (APIs, secrets, IAM, WIF)
│   └── first_deploy.sh       # first build, push and Cloud Run deploy
├── .github/workflows/
│   ├── ci.yml                # run tests on every push and pull request
│   └── cd.yml                # test, build image, deploy to Cloud Run on push to main
├── docs/
│   ├── architecture.png      # system architecture
│   ├── graph.png             # LangGraph workflow
│   └── screenshots/          # UI screenshots used above
├── .streamlit/               # theme config and secrets template
├── Dockerfile
├── requirements.txt
├── pytest.ini
└── .env.example
```

## Getting started

```bash
git clone https://github.com/<username>/AI-BASED-EMAIL-ASSISTANT.git
cd AI-BASED-EMAIL-ASSISTANT

python -m venv .venv
.venv\Scripts\activate            # Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env              # add GROQ_API_KEY
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # add Google OAuth values
streamlit run app.py
```

Draft-only mode works with just a Groq API key. Google sign-in additionally needs an OAuth client:

1. In Google Cloud Console, enable the **Gmail API**.
2. Configure the **OAuth consent screen** (External) with scopes `openid`, `userinfo.email`, `userinfo.profile` and `gmail.send`, and add yourself as a **test user**.
3. Create an **OAuth client ID** (Web application) with redirect URIs `http://localhost:8501` and your deployed URL.

**Optional tracing:** set `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY` and `LANGSMITH_PROJECT=quill` in `.env`.

**Docker:** `docker build -t quill .` then `docker run -p 8080:8080 --env-file .env quill`.

## Testing and CI/CD

```bash
python -m pytest -v
```

The suite covers routing, address extraction and validation, the guarantee that the LLM cannot change the recipient, prompt rules, OAuth `state` signing, message building (Cc/Bcc/attachments) and the structured-output retry logic. None of it needs an API key or network access.

- **CI** (`ci.yml`) runs the tests on every push and pull request.
- **CD** (`cd.yml`) runs the tests, builds the Docker image, pushes it to Artifact Registry and deploys it to Cloud Run on every push to `main`.

## Deployment

Containerised with Docker and deployed on **Google Cloud Run** (scale-to-zero, max 2 instances, secrets injected from Secret Manager). One-time setup is scripted in `deploy/setup_gcp.sh` and `deploy/first_deploy.sh`; afterwards every push to `main` deploys automatically.

## Limitations and roadmap

**Current limitations**
- Google sign-in is restricted to test users until the app passes Google's verification (`gmail.send` is a sensitive scope). Draft-only mode is available to everyone.
- SQLite checkpoints live on the container's disk, so in-progress drafts do not survive a restart or move between instances.
- The service scales to zero, so the first request after idle time takes a few seconds.
- The AI can still make mistakes, which is why review is mandatory.

**Roadmap**
- Per-user and global daily quotas in Firestore (the current limit is per session)
- Google OAuth verification for public sign-in
- Postgres checkpointer for durable state
- Long-term memory (preferred tone, signature, frequent contacts) using the LangGraph Store
- Job-application templates that attach and reference a saved resume

## Author

**Akanshu Mittal**
GitHub: <your-github-url> · LinkedIn: <your-linkedin-url>
