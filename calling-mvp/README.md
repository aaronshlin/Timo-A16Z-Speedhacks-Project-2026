# Calling tools

The backend is `app.main:app`. These scripts launch that same app, sign in locally to Dial, verify callbacks, and preserve the existing `first_call.py --call` command.

See [CALENDAR_TRIGGERS.md](CALENDAR_TRIGGERS.md) for Google sign-in, the dry-run/restart checks, automatic polling and manual dialing.

From the repository root:

```bash
uv sync
```

From `calling-mvp`, install local tools (Node 22.19+):

```bash
npm install --prefix .tools --no-audit --no-fund @getdial/cli@0.56.0 cloudflared@0.7.3
XDG_DATA_HOME="$PWD/.local/share" bash tools.sh dial auth login YOUR_EMAIL
../.venv/bin/python verify_login.py
```

Use the latest emailed code at the hidden prompt. Keep credentials, codes, enrolled phone, private configuration, state and logs out of Git. Set `USER_PHONE` in private `calling-mvp/.env` or `DEMO_PHONE` in the main `.env` before launching. The current laptop already has the local `.venv`, tools and login prepared.

Calls are disabled by default. Real reads do not enable calls or writes. The live launcher requires explicit `--live`, a verified signed callback, and the persistent one-call demo budget. SMS and calendar writes are disabled; actions and receipts are demo-only.
