# NimbusPay × Guardrails AI — interactive demo

A visual companion to the course notebook
[`Notebook/Guardrails ai/guardrails_nimbuspay_v3.ipynb`](Notebook/Guardrails%20ai/guardrails_nimbuspay_v3.ipynb).
Learners see each Guardrails flow run **live** here, then read the code in the notebook.

The app demonstrates, on a fictional fintech support bot:

- **OnFail Playground** — the five `on_fail` actions (FIX / FILTER / REFRAIN / EXCEPTION; REASK in the Structured tab).
- **Validator Gallery** — real Hub validators (`DetectPII`, `CompetitorCheck`), LLM-as-judge toxicity & topic checks, and real custom validators (`@register_validator` + a parameterised `Validator` subclass).
- **Input Guard** — block PII/injection in the user's prompt *before* it reaches the model.
- **Structured Parsing** — turn a messy customer email into a typed object, with an under-the-hood `guard.history` reveal and a live REASK demo.

## Architecture notes (why it's built this way)

Streamlit Community Cloud gives each app **1 GB RAM** and only runs
`pip install -r requirements.txt`. So:

- **No torch.** The Hub's `ToxicLanguage` / `GibberishText` validators pull PyTorch and
  would blow the memory cap. Toxicity & topic use **LLM-as-judge** calls to Groq instead.
- **Hub validators install at runtime.** `guardrails hub install` is a CLI step Cloud
  won't run for you, so `guards.setup_guardrails()` runs it once per cold start (cached).
  If it fails, the app falls back to regex/substring matchers and **keeps working**.
- **Pinned `guardrails-ai==0.10.0`.** Version 0.10.1 was a malicious supply-chain upload
  (CVE-2026-45758) and was pulled; 0.10.0 is clean.

## Run locally

```bash
# 1. Install deps (a venv is recommended)
pip install -r requirements.txt

# 2. Add your keys
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
#   then edit .streamlit/secrets.toml with real values

# 3. Launch
streamlit run app.py
```

Keys needed:
- `GROQ_API_KEY` — https://console.groq.com/keys
- `GUARDRAILS_TOKEN` — https://hub.guardrailsai.com (free; optional — without it the app
  uses regex/substring fallbacks for PII/competitor checks).

## Deploy to Streamlit Community Cloud

1. **Push to GitHub.** Commit everything *except* `.streamlit/secrets.toml` (already
   git-ignored). The repo root must contain `app.py` and `requirements.txt`.
2. Go to **share.streamlit.io** → **New app** → pick your repo/branch, set
   **Main file path** to `app.py`.
3. Open **Advanced settings → Secrets** and paste:
   ```toml
   GROQ_API_KEY = "gsk_..."
   GUARDRAILS_TOKEN = "..."
   ```
4. **Deploy.** First cold start is slower — it installs the Hub validators. Watch the
   sidebar **Status** panel: each validator shows `✅ real` or `↩️ fallback`.

> ⚠️ Live calls consume **your** Groq quota. For a public class link, consider adding a
> "bring-your-own-key" `st.text_input` in the sidebar so learners use their own key.

## Files

| File | Purpose |
|------|---------|
| `app.py` | Streamlit UI — five tabs (`st.tabs`) |
| `guards.py` | Validators, LLM-judge, cached Hub setup, Pydantic models |
| `requirements.txt` | Pinned, torch-free dependencies |
| `.streamlit/config.toml` | Theme + server options |
| `.streamlit/secrets.toml.example` | Template for local keys |
