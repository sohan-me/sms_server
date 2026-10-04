// pm2 start ecosystem.config.js
// pm2 logs orbitalcore-sms
//
// env here is what reaches the gunicorn workers — a .env file does not.
// Do not start app.py directly: its __main__ runs the Werkzeug dev server with
// debug=True, whose reloader opens a duplicate Redis subscriber.

module.exports = {
  apps: [
    {
      name: "orbitalcore-sms",
      // Via the interpreter: a copied venv keeps absolute-path shebangs in bin/,
      // so venv/bin/gunicorn can point at a machine that no longer exists.
      script: "venv/bin/python",
      args: "-m gunicorn -c gunicorn.conf.py app:app",
      cwd: __dirname,
      interpreter: "none",
      env: {
        // No Redis. OTP delivery runs entirely through the shared database:
        // every worker polls otp_message and pushes to its own WebSocket clients,
        // so all users are served regardless of which worker ingested the SMS.
        // Adding REDIS_URL here only makes delivery ~180ms faster; it is not
        // required, and `redis` need not even be installed.

        // Must be identical across workers or admin sessions break.
        SECRET_KEY: "CHANGE-ME-to-a-long-random-shared-value",

        // MUST be identical across workers — they share the OTP table through it.
        // SQLite serialises writers; switch to PostgreSQL for real multi-worker.
        DATABASE_URL: "sqlite:///device_auth.db",

        // Optional global token that bypasses per-user DeviceUser lookup.
        // WS_AUTH_TOKEN: "",

        OTP_RETENTION_MINUTES: "30",
        BIND: "0.0.0.0:8002",

        // Capacity rule: every open WebSocket permanently occupies one thread.
        // Total concurrent WebSocket connections must stay below
        // WEB_CONCURRENCY * WEB_THREADS, and headroom is needed for
        // POST /api/messages. 4 * 16 = 64 slots here.
        WEB_CONCURRENCY: "4",
        WEB_THREADS: "16",

        // Server-side WebSocket ping; reaps clients killed by nginx or a NAT
        // timeout instead of leaking their thread.
        WS_PING_INTERVAL: "25",

        // How often each worker checks otp_message for OTPs ingested by another
        // worker. 0.5s measured median ~188ms end-to-end. Lower = faster, more DB load.
        OTP_POLL_INTERVAL: "0.5",
      },
      autorestart: true,
      max_restarts: 10,
      time: true,
    },
  ],
};
