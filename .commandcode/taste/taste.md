# General

- Communicates casually and directly ("Bro", informal); match that tone, skip formality. Confidence: 0.85
- Works on the OrbitalCore OTP server project (Flask + WebSocket OTP delivery, per-user WSS tokens, admin portal). Confidence: 0.9
- Intended OTP flow (his stated design): HTTP ingest → normalize OTP → store keyed by phone number → push to WS clients authenticated by token and matched against their registered numbers. Redis Pub/Sub is the cross-worker fan-out. He has since floated dropping the OTP DB model and keeping OTPs in Redis with a ~10-minute auto-delete instead — still an open question, not a settled decision. Confidence: 0.8
- Designs for multiple users from the start. Proposed auth flow: per-user token issued after username + password + device-fingerprint authentication (broader than the fingerprint-only, admin-gated approval currently built). Confidence: 0.7
- Consider alternative stacks for the SMS server (proposed FastAPI + Tortoise ORM when a bug seems architectural). Even after being told a rewrite wouldn't change the bug, he kept asking "what's your plan with FastAPI & Tortoise?" — he stays curious about the alternative rather than closing it. Confidence: 0.75
