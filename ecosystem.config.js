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
        // Required. Without it each worker only delivers OTPs to its own clients.
        REDIS_URL: "redis://127.0.0.1:6379/0",

        // Must be identical across workers or admin sessions break.
        SECRET_KEY: "CHANGE-ME-to-a-long-random-shared-value",

        // SQLite serialises writers. Switch to PostgreSQL for real multi-worker.
        DATABASE_URL: "sqlite:///device_auth.db",

        // Optional global token that bypasses per-user DeviceUser lookup.
        // WS_AUTH_TOKEN: "",

        OTP_RETENTION_MINUTES: "30",
        BIND: "0.0.0.0:8002",
        WEB_CONCURRENCY: "4",
        WEB_THREADS: "8",
      },
      autorestart: true,
      max_restarts: 10,
      time: true,
    },
  ],
};
