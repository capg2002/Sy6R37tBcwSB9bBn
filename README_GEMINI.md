# Gemini advisory-only replacement

This replaces the OpenAI advisor with Google's Gemini Developer API, using
`gemini-2.5-flash-lite`. Google lists free input/output for this model as of
2026-10-14. Free eligibility and quotas are project-specific and can change.
Use an AI Studio project on the Free tier without paid billing enabled. The
Python program cannot verify or enforce your Google account's billing tier.
There is no automatic paid-model fallback.

## Copy files
Stop only the advisor if it is running. Merge these files into your project:
- Replace advisor.py, advisor.json and requirements-advisor.txt at the root.
- Replace services/llm_advisor.py and tests/test_advisor.py.
- services/advisor_log.py and services/advisor_context.py are unchanged copies
  included for users who have not yet copied the original advisor package.
- Keep your current paper_loop.py (the uploaded paper_loop(3).py already includes
  advisory capture), engine, strategies, Gmail, Telegram and trading config.
- Keep any existing SQLite files. No database migration is needed.

## Credential setup
1. Open https://aistudio.google.com/apikey and create a Gemini API key in a
   project with Free-tier access. Check its tier and quotas in AI Studio.
2. Add this to your existing root .env:

   GEMINI_API_KEY=your_actual_key

This is separate from your Sheets service-account JSON. Nothing new goes in
secrets/. OPENAI_API_KEY is no longer read and can be removed if unused elsewhere.
Do not upload .env or commit it to git.

## Install and test
From the project root with your virtual environment active:

    python -m pip install -r requirements-advisor.txt
    python -m unittest discover -s tests -p test_advisor.py -v

These 11 tests use mocks and temporary SQLite databases, not Google's API.

For a real API smoke test WITHOUT a paper database:

    python test_gemini_connection.py

This sends one fixed synthetic market snapshot through the SAME GeminiAdvisor
class, prints the result, and exits. Each invocation makes one API request and
consumes quota. It does not use the daily ledger limit and never submits orders.

## Run with your trader
If no paper database exists, start your existing trader:

    python paper_loop.py --db runtime/paper.sqlite --sheets --telegram

This creates/initializes the paper database and starts paper trading. Keep it
running. Wait for a new completed-candle decision and the GARCH calculation.
Then in another terminal:

    python advisor.py --db runtime/paper.sqlite --input
    python advisor.py --db runtime/paper.sqlite --once
    python advisor.py --db runtime/paper.sqlite --show

For ongoing observation:

    python advisor.py --db runtime/paper.sqlite --watch

--input previews the locally saved context, which is MORE detailed than the
allowlisted snapshot sent to Google. No Google request is made by --input/--show.
Responses are saved in runtime/paper.sqlite.advisor.sqlite. The ledger reads the
paper database read-only. Recommendations cannot alter modes, balances or orders.
Previously processed candles are not retried when changing provider; wait for a
new candle. Preserve both databases.

## Quotas and errors
Default limit: 10 attempts per UTC day in this advisor ledger. This is a local
cap, not a claim about Google's quota. Existing attempts count toward this cap.
To cover all 24 hourly candles, first check your project's actual request limit,
then set max_requests_per_utc_day to 24 and restart only the advisor.

429 becomes an error with error_type GeminiRateLimitError. The same candle is
not retried; later eligible candles can try again within the local cap. Network,
authentication and HTTP failures are sanitized. No raw credential-bearing error
body is logged. A quota/API failure does not stop trading. Request timeouts bound
network waits, not a strict real-time deadline. Only one advisor worker may run.

## Information sent and limitations
Google's unpaid-service terms permit using submitted content and responses to
improve products. Use non-confidential demonstration data. The adapter sends
only allowlisted numeric market indicators, forecast sigma and the current mode.
It does NOT send balances, holdings, trades, keys, strategy settings, config hashes,
or free-form strings from your configuration. Locally saved advisor records still
contain the original full context; protect your local runtime files.

A sigma is hourly volatility in fractional units (0.01 means 1%), not a direction
forecast. The prompt and all recommendations are advisory-only. JSON validation
checks structure, not financial correctness. No trading benefit is established.
The model has no tools, browsing, order API or news feed. The compact input means
it cannot assess your account's exposure or detailed strategy parameters.

Official references:
https://ai.google.dev/gemini-api/docs/pricing
https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash-lite
https://ai.google.dev/gemini-api/docs/generate-content/structured-output
https://ai.google.dev/gemini-api/docs/rate-limits
https://ai.google.dev/gemini-api/terms
