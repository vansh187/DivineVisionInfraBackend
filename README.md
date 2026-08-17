# DivineVisionInfraBackend - Auth API

This workspace provides database init script and a FastAPI app implementing signup/login for customers and brokers.

Quick setup:

1. Copy `.env.example` to `.env` and set `JWT_SECRET_KEY` and `DATABASE_URL`.
2. Initialize the database (Postgres):

```bash
psql "${DATABASE_URL}" -f db_init.sql
```

3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Run the API:

```bash
uvicorn DivineAPI.main:app --reload
```

Endpoints:
- POST /customer/signup
- POST /customer/login
- POST /broker/signup
- POST /broker/login
- POST /chatbot/session/init - starts a visitor chat session, no login required
- POST /chatbot/message - send a text/voice message or trigger the callback-request flow, no login required
- GET /chatbot/callback-requests - broker/admin-only list of pending callback requests

The chatbot module needs `GEMINI_API_KEY` and `GROQ_API_KEY` set (see `.env.example`).
Knowledge base articles are ingested with `python -m scripts.ingest_kb <file.txt> --title "..." --category pricing`.

Notes:
- Tables are `DIVINE_CUSTOMER_USERS` and `DIVINE_BROKER_USERS` and the SQL script targets PostgreSQL.
- Tokens are JWT signed with `JWT_SECRET_KEY` from `.env`.
- Passwords are bcrypt-hashed using passlib.
- A basic in-memory rate limiter is applied (10 requests per minute per IP per path).
# DivineVisionInfraBackend
Python Backend For Divine vision infra
