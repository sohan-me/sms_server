# Code Style

- Do NOT litter source files with verbose comments and multi-line docstrings explaining the obvious. User pushed back with "remove too much docs from python files." Keep only load-bearing "why" comments — the non-obvious constraint or bug the code would otherwise trip over (e.g. why `gthread` is required, why WAL needs its own connection, why bootstrap retries on a race). Long rationale belongs in docs.md, not in the file. Applies to config files too (`gunicorn.conf.py`, `ecosystem.config.js`). Confidence: 0.85
- Trimming comments is a code change like any other: re-verify it compiles and the test suite still passes before reporting done. Confidence: 0.7
