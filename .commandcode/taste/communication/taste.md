# Communication

- Docs (docs.md) must be short, simple, minimal, and easy — written so an end user can hand it directly to their agent. User has asked for this repeatedly. Confidence: 0.95
- Prefers concrete examples over abstract descriptions (e.g., sample JSON responses for a stated number of numbers/OTPs). Confidence: 0.8
- Use live production endpoints (e.g., wss://server.orbitalcore.site) in docs and examples, not placeholders. Confidence: 0.8
- Apply known deployment facts before giving advice: when the setup is already established (single VPS, local Redis), answer only for that setup — don't hedge with irrelevant alternatives (host IPs, LAN binding, multi-machine config) or the user will push back ("listen its deployed on VPS... why i need host ip?"). Confidence: 0.8
- Poses challenges as sharp, short, logic-probing questions rather than instructions: "if redis is the issue then how its working for admin?", "why i need host ip?", "something is wrong here", "what i need to do on VPS after pulling the repo? just restart app with pm2?". Expect and welcome these — they're the user stress-testing the diagnosis, not rejecting it. Answer the contradiction head-on with the mechanism.
- His questions often embed a *presumed answer* ("just restart with pm2?") that is wrong. Refute the premise in the very first sentence — "Not just a restart, `pm2 restart` re-runs the old command" — then give the real steps. Never answer the question as asked or reply "yes" and move on. Confidence: 0.8
- Speaks in run-on, unpunctuated English with typos; interpret intent liberally and answer the underlying question rather than the literal phrasing. Confidence: 0.8
- Interrupts with a "listen..." or "if ... then why ..." correction the moment advice drifts from his actual setup — check his stated environment before giving any prescriptive advice. Confidence: 0.8
