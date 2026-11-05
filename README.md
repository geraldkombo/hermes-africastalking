# hermes-africastalking

**Hermes Agent on any phone — SMS + USSD via Africa's Talking.** 📱

A platform-adapter plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent)
that opens a channel no other agent plugin reaches: **feature phones**. No internet,
no app, no smartphone — dial a USSD code or send an SMS, and Hermes answers.
Built for the 1B+ people across Africa and Asia whose only connected device is a
basic handset.

```
Phone (USSD/SMS) ⇄ Africa's Talking cloud ⇄ HTTP callbacks ⇄ this plugin's webhook
server (localhost) ⇄ Hermes gateway ⇄ agent          Outbound: AT REST API (stdlib)
```

## What you get

- **SMS two-way** — text the shortcode; the message runs a full Hermes agent turn
  and the reply is sent back as SMS. Long replies are split on word boundaries
  into 160-char parts, hard-capped so an agent essay can never become an
  unbounded phone bill.
- **USSD menu** — a session menu (`Ask Hermes a question` / `About` / `Exit`).
  USSD callbacks must answer in seconds while agent turns take longer, so the
  menu acknowledges instantly and the answer follows by SMS — the same
  hand-off pattern real telco services use.
- **Cron delivery** — `deliver=africastalking` jobs and `hermes send` route to
  `AT_HOME_CHANNEL` through a standalone sender (no gateway process needed).
- **Setup wizard** — appears in `hermes gateway setup`, and a bundled
  `africastalking-setup` skill walks the agent (and you) through tunnels and
  callback URLs.
- **Free to try** — everything works on the Africa's Talking
  [sandbox](https://account.africastalking.com/auth/register) (free account,
  simulated shortcode + phone simulator). Production shortcodes are the user's
  own AT account.

## Install

From the Hermes plugin catalog (once listed):

```
hermes plugins install africastalking
```

Or manually — clone into the plugins directory:

```
git clone https://github.com/geraldkombo/hermes-africastalking ~/.hermes/plugins/africastalking-src
# the plugin itself is the africastalking/ subdir:
cp -r ~/.hermes/plugins/africastalking-src/africastalking ~/.hermes/plugins/africastalking
hermes plugins enable africastalking
```

## Quickstart (sandbox, ~10 minutes)

1. **AT account:** register free at
   [account.africastalking.com](https://account.africastalking.com/auth/register),
   open the **sandbox** app, copy the API key (Settings → API Key).
2. **Configure:** `hermes gateway setup` → Africa's Talking, or set env vars in
   `~/.hermes/.env`:

   ```
   AT_USERNAME=sandbox
   AT_API_KEY=<your sandbox API key>
   AT_WEBHOOK_SECRET=<any long random string>
   AT_ALLOWED_USERS=+2547xxxxxxxx     # phones allowed to talk to the agent
   ```

3. **Tunnel the callback server** (AT's cloud must reach it; free, no account):

   ```
   cloudflared tunnel --url http://127.0.0.1:8055
   ```

4. **Set callback URLs** in the AT dashboard (SMS and USSD sections):

   ```
   https://<your-tunnel>/sms?secret=<AT_WEBHOOK_SECRET>
   https://<your-tunnel>/ussd?secret=<AT_WEBHOOK_SECRET>
   ```

5. **Restart and test:** `hermes gateway restart`, then use the sandbox phone
   simulator — send an SMS or dial the USSD code and watch Hermes reply.

Full walkthrough + troubleshooting: ask Hermes to load the bundled
`africastalking: africastalking-setup` skill, or see
[SKILL.md](africastalking/skills/africastalking-setup/SKILL.md).

## Configuration reference

| Env var | Required | Default | Purpose |
|---|---|---|---|
| `AT_USERNAME` | yes | — | AT app username (`sandbox` for the sandbox app) |
| `AT_API_KEY` | yes | — | AT app API key |
| `AT_SENDER_ID` | no | — | sender ID/shortcode replies come from (production; sandbox ignores it) |
| `AT_CALLBACK_HOST` | no | `127.0.0.1` | interface the webhook server binds to |
| `AT_CALLBACK_PORT` | no | `8055` | webhook server port |
| `AT_WEBHOOK_SECRET` | recommended | — | required as `?secret=…` on callback URLs |
| `AT_ALLOWED_USERS` | no | — | comma-separated phone allowlist (gateway pairing/authorization) |
| `AT_ALLOW_ALL_USERS` | no | `false` | let anyone reaching the shortcode use the agent (sandbox only) |
| `AT_HOME_CHANNEL` | no | — | phone number for cron/notification delivery |

The same keys work under `gateway.platforms.africastalking.extra` in
`config.yaml` (env wins).

## Security & disclosures

What this plugin does at runtime — everything else it does *not* do:

- **Network calls:** outbound HTTPS to `api.africastalking.com` only (sending
  SMS), plus inbound HTTP callbacks that Africa's Talking posts to the local
  webhook server.
- **Listens on a local port:** the callback server binds `127.0.0.1:8055` by
  default. Exposing it publicly (tunnel) is the user's explicit step; set
  `AT_WEBHOOK_SECRET` so only AT's callbacks (with `?secret=…`) are accepted —
  the check is constant-time.
- **Credentials:** read only through Hermes's scoped-secret routes
  (`requires_env` + `get_scoped_secret`); never from files, never logged. Phone
  numbers are masked in logs (`pii_safe`).
- **Cost:** the plugin itself is free; sending SMS/USSD in production is metered
  by Africa's Talking on the user's own account. Sandbox testing costs nothing.
  Replies are capped (3 SMS parts by default) to bound spend.
- **No background processes** beyond the gateway's own lifecycle, no telemetry,
  no self-updating, no shell-outs, no dependencies beyond Hermes core
  (the AT client is pure Python stdlib; the webhook server uses core's aiohttp).

## Development

```
python -m unittest discover -s africastalking/tests -t . -v   # 53 offline tests, no network
hermes plugins validate africastalking --install-deps         # catalog CI check → ok: true, 15/15
```

Layout:

```
africastalking/
├── plugin.yaml      # manifest (kind: platform, requires_env, requires_hermes)
├── __init__.py      # import-light entry: register(ctx) → platform + bundled skill
├── adapter.py       # AfricaTalkingAdapter: webhook server, send, cron sender, setup wizard
├── at_client.py     # AT REST client + callback parsers (pure stdlib)
├── ussd.py          # USSD session state machine (pure, offline-testable)
├── adapter_hint.py  # system-prompt hint (plain text, short replies)
├── skills/africastalking-setup/SKILL.md
└── tests/           # offline suite: parsers, chunking, state machine, send paths
```

## Limitations (honest ones)

- Text only — no MMS/media inbound or outbound (SMS doesn't carry it).
- USSD sessions can't wait for a slow agent turn; questions hand off to SMS.
- Inbound needs a public callback URL (tunnel) — AT pushes, it never polls.
- Sandbox apps only deliver SMS to sandbox-verified numbers.

## License

MIT — see [LICENSE](LICENSE). Not affiliated with Africa's Talking or Nous Research.
