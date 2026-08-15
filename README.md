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

Notes:
- Tables are `DIVINE_CUSTOMER_USERS` and `DIVINE_BROKER_USERS` and the SQL script targets PostgreSQL.
- Tokens are JWT signed with `JWT_SECRET_KEY` from `.env`.
- Passwords are bcrypt-hashed using passlib.
- A basic in-memory rate limiter is applied (10 requests per minute per IP per path).
# DivineVisionInfraBackend
Python Backend For Divine vision infra
