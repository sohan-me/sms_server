// pm2 process definition for the OrbitalCore SMS/OTP server.
//
// Run:  pm2 start ecosystem.config.js
// Logs: pm2 logs orbitalcore-sms
//
// pm2's `env` block is what actually reaches the gunicorn master and every
// forked worker. A .env file does NOT — gunicorn does not load one, and
// python-dotenv is not used here.
//
// Do NOT start app.py directly. Its __main__ block runs the Werkzeug dev server
// with debug=True, whose reloader spawns a second process that re-imports app
// and opens a duplicate Redis subscriber.

module.exports = {
  apps: [
    {
      name: "orbitalcore-sms",
      // Invoked via the venv interpreter rather than the venv/bin/gunicorn
      // script: a copied venv keeps hardcoded absolute-path shebangs in its
      // bin/ scripts, so those can point at a different machine entirely.
      script: "venv/bin/python",
      args: "-m gunicorn -c gunicorn.conf.py app:app",
      cwd: __dirname,
      interpreter: "none",
      env: {
        // Required: without it, each worker delivers OTPs only to the WebSocket
        // clients that share its process. See /api/health -> redis_subscriber_alive.
        REDIS_URL: "redis://127.0.0.1:6379/0",

        // Required: must be identical across workers or admin session cookies
        // break when the browser is routed to a different worker.
        SECRET_KEY: "CHANGE-ME-to-a-long-random-shared-value",

        // Move off the SQLite default before running multiple workers:
        // concurrent writes raise "database is locked" (WAL + busy_timeout are
        // enabled as a mitigation, not a fix).
        DATABASE_URL: "sqlite:///device_auth.db",

        // Optional. A global token that bypasses per-user DeviceUser lookup.
        // WS_AUTH_TOKEN: "",

        OTP_RETENTION_MINUTES: "30",
        WEB_CONCURRENCY: "4",
        WEB_THREADS: "8",
      },
      autorestart: true,
      max_restarts: 10,
      time: true,
    },
  ],
};
