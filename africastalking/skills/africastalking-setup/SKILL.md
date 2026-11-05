---
name: africastalking-setup
description: Connect Hermes to a phone via Africa's Talking (SMS + USSD). Use when the user wants to set up the africastalking platform, run the callback tunnel, configure SMS/USSD callback URLs, test in the free Africa's Talking sandbox, or go live on a real shortcode.
---

# Africa's Talking setup

Goal: make this Hermes gateway reachable from any phone — SMS two-way plus a
USSD menu — through Africa's Talking (AT). The free sandbox is enough for the
whole setup; a production shortcode is only needed to go live.

## 1. Create the AT app (free)

1. Register at https://account.africastalking.com/auth/register (no card needed).
2. Create an app. For testing, use the **sandbox** app (username `sandbox`).
3. Copy the **API key**: app → Settings → API Key.

## 2. Configure Hermes

Set these (the `hermes gateway setup` wizard does it interactively, or write
them into `~/.hermes/.env` directly):

| Var | Value |
|---|---|
| `AT_USERNAME` | your AT app username (`sandbox` for the sandbox app) |
| `AT_API_KEY` | the API key from step 1 |
| `AT_WEBHOOK_SECRET` | any long random string — **strongly recommended** |
| `AT_CALLBACK_PORT` | default `8055`; change only if taken |
| `AT_ALLOWED_USERS` | comma-separated phone numbers allowed to use the agent (e.g. `+254712345678`). On a public shortcode always set this, or set `AT_ALLOW_ALL_USERS=true` only for sandbox play |
| `AT_HOME_CHANNEL` | phone number for cron/notification delivery (optional) |

Then restart the gateway (`hermes gateway restart`). Check the log for:
`Africa's Talking: callback server on 127.0.0.1:8055`.

## 3. Expose the callback server

AT's cloud must reach the local webhook server. Easiest free option — a
cloudflared quick tunnel (no account):

```
cloudflared tunnel --url http://127.0.0.1:8055
```

It prints a public URL like `https://random-words-1234.trycloudflare.com`.
Any tunnel works (ngrok, tailscale funnel, a VPS reverse proxy).

## 4. Point AT at the tunnel

In the AT dashboard → your app → **SMS** settings, set the delivery/callback
URL, and under **USSD** set the callback URL:

```
SMS  callback: https://<your-tunnel>/sms?secret=<AT_WEBHOOK_SECRET>
USSD callback: https://<your-tunnel>/ussd?secret=<AT_WEBHOOK_SECRET>
```

If `AT_WEBHOOK_SECRET` is unset, drop the `?secret=…` part — but then anyone
who learns the tunnel URL can inject messages into the agent, so prefer the
secret.

## 5. Test

- **Sandbox simulator:** AT dashboard → Sandbox → launch the phone simulator;
  send an SMS to the sandbox shortcode and watch Hermes reply. USSD: dial the
  simulated shortcode to get the menu.
- **Real phone (production only):** needs a live AT app with a registered
  shortcode/alphanumeric sender ID and the callbacks configured identically.

## Troubleshooting

| Symptom | Fix |
|---|---|
| No reply to SMS | Gateway log shows the callback? No → tunnel/URL wrong. Yes but no SMS out → check `AT_API_KEY`, and sandbox apps only send to sandbox-verified numbers |
| USSD menu flashes and closes | The callback didn't answer in time — check tunnel latency; AT gives up after a few seconds |
| `403 forbidden` in tunnel log | `?secret=` missing or doesn't match `AT_WEBHOOK_SECRET` |
| Duplicate agent replies | AT retried an unacked callback — the adapter dedups for 10 min; if it persists, check two gateway instances aren't sharing one tunnel |
| Port already in use | Set a different `AT_CALLBACK_PORT` (and update the tunnel command) |

## Going live (cost note)

Production SMS/USSD on AT is metered per message by Africa's Talking and the
telcos — that is the user's own AT account, not this plugin. Sandbox testing is
free. Set `AT_ALLOWED_USERS` before exposing a production shortcode.
