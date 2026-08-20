# Local Patches Manifest (HERMES-OWNED)

This document is the authoritative inventory of local modifications maintained on
top of upstream `nesquena/hermes-webui`. A cron agent (`webui-merge-maintenance`)
uses this file to safely merge upstream releases with our patches. **Never delete
this file during a merge.**

Canonical fork: `github.com/FEI352/hermes-webui` (branch `master`).
Upstream: `github.com/nesquena/hermes-webui` (remote `upstream`).

---

## Rules of engagement for the merge cron agent

1. The live deployment lives in `/root/hermes-webui` (served by `hermes-webui.service`,
   port 8787, proxied by OpenResty as `hermes.fja.su`). Merge in a TEMP clone, never in-place.
2. After `git fetch upstream`, merge `upstream/master` into the temp clone, resolve conflicts
   per the per-file policy below, run the sanity checks, push to `origin/master`, then
   `git pull --ff-only` in `/root/hermes-webui` and `systemctl restart hermes-webui.service`.
3. If merge or sanity checks fail: `git merge --abort` in the temp clone, do NOT push,
   do NOT touch the live dir, and report the failure with the conflict hunks.
4. Local features are LOYALTY-PROTECTED: on conflict, preserve the local semantic for the
   protected files/features listed below, even at the cost of dropping an upstream change.

---

## Feature inventory (all local, all loyalty-protected)

### F1. dsh-style bottom StatsLine bar (stats: turns · msgs · LLM time · tok/s · cache % · in/out tokens · cost)
- Files: `static/ui.js` (StatsLine + `_syncStatsLine`, `_fmtCostCompact`, `_fmtTokensCompact`),
  `static/i18n.js` (`stats_*` keys, zh + en), `static/index.html` (`#statslineWrap`),
  `static/style.css` (`.statsline-wrap` styles), `static/messages.js` (per-message cost),
  `static/sessions.js` (per-session cost), `static/workspace.js` (minor).
- Merge policy: **LOCAL-FIRST**. Upstream may restructure the stats/bottom bar; if so, re-apply
  our StatsLine block on top. Keep both zh and en i18n keys complete.

### F2. Time-context injection per turn (dsh port)
- Files: `api/time_context.py` (new file, keep whole), `api/streaming.py`, `api/routes.py`,
  `server.py` (wire time-context into the outgoing user message + `X-Client-Timezone` header).
- Merge policy: **LOCAL-FIRST**. If upstream changes message-building code, re-apply the
  `time_context` call at the same boundary. Keep `client_timezone_var` ContextVar plumbing.

### F3. Usage / cost plumbing (estimated_cost reaches the UI)
- Files: `api/routes.py`, `api/gateway_chat.py`, `api/session_ops.py`, `api/models.py` etc.
- Merge policy: **LOCAL-FIRST**. Keep `estimated_cost` fields in usage payloads
  (`usage.get("estimated_cost") or usage.get("estimated_cost_usd") or 0`).

### F4. Provider config / profile tolerance
- Files: `api/config.py`, `api/profiles.py`.
- Merge policy: **LOCAL-FIRST on tolerance** — unknown provider config keys
  (`api_key_2`, `reasoning_efforts`, `reasoning_effort`) must be ignored, not rejected;
  profile creation/listing must tolerate these keys.

---

## Sanity checks (must all pass before push)

```bash
cd <temp clone>
python3 -m py_compile server.py api/*.py       # compile all server-side code
node --check static/ui.js static/i18n.js static/messages.js static/sessions.js static/boot.js static/workspace.js  # JS syntax
grep -q "statslineWrap" static/index.html      # F1 present
grep -q "def .*time_context\|Time sampled while preparing" api/time_context.py  # F2 present
grep -q "estimated_cost" api/routes.py         # F3 present
```

## Verifying the deployed result (post-ff-pull + restart)
- `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8787/login` → 200/302 with no 500.
- Open `hermes.fja.su` → bottom StatsLine renders after first message turns;
  cost shows `~$0.0X` (requires the agent-side pricing patch in Hermes,
  `/usr/local/lib/hermes-agent/agent/usage_pricing.py`, protected by `protect_hermes_patches.py`).
- First message of a session starts with a `Time sampled while preparing turn …` block.
