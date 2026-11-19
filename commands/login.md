---
description: Sign in to Strobes (browser approval, no API key needed)
argument-hint: "[strobes-url] e.g. https://acme.strobes.co"
---
Sign the Strobes agents MCP server in.

1. Call `strobes_whoami`. If it's already connected, tell me who I'm signed in as and stop.
2. Otherwise call `strobes_login` (pass `url` = "$ARGUMENTS" if I gave one).
3. Show me the `verification_url` as a clickable link and the `user_code` in bold, and tell me
   to confirm the code matches, pick my organization and click **Authorize**.
4. Call `strobes_login_status` (wait_seconds 120) until it's `approved`, `denied` or `expired`,
   then confirm with `strobes_whoami`.
