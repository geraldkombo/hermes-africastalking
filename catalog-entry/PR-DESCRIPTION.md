# Catalog PR description draft — plugin-catalog/africastalking.yaml

(For the PR to NousResearch/hermes-agent. Rule 13 disclosures included below, as
reviewers expect them summarized on the entry PR.)

---

## What it does

**africastalking** is a platform adapter (`kind: platform`) that makes Hermes
reachable from **any phone** — feature phones included — via Africa's Talking:

- **SMS two-way**: an inbound text runs a full agent turn; the reply goes back
  as SMS, split on word boundaries into 160-char parts with a hard cap
  (3 parts by default) so a long answer can't become an unbounded phone bill.
- **USSD menu**: dial → menu → "Ask Hermes a question" → type. USSD callbacks
  must answer within seconds while agent turns take longer, so the menu
  acknowledges instantly and the answer follows by SMS (the same
  ack-now/deliver-later pattern the adapter guide documents for WeCom's
  response window).
- **Cron delivery**: `deliver=africastalking` works out-of-process via
  `standalone_sender_fn`; home target from `AT_HOME_CHANNEL`.
- **Setup wizard** in `hermes gateway setup` + a bundled `africastalking-setup`
  skill (tunnel + callback URL walkthrough, troubleshooting table).

Zero competition in the catalog today: no USSD channel, no Africa's Talking
entry (checked across all 979 entries). It reaches users no other channel can:
no internet, no smartphone, no app.

## Surfaces used (public API only)

- `ctx.register_platform(...)` with: adapter_factory, check_fn, validate_config,
  is_connected, required_env, install_hint, setup_fn, env_enablement_fn,
  cron_deliver_env_var, standalone_sender_fn, allowed_users_env, allow_all_env,
  max_message_length, platform_hint, emoji, pii_safe, allow_update_command.
  `allowlist_suppresses_pairing` is feature-detected via
  `dataclasses.fields(PlatformEntry)` for older cores.
- Public imports only: `gateway.platforms.base` (BasePlatformAdapter,
  SendResult), `gateway.platforms.event` (MessageEvent, MessageType),
  `gateway.platforms.helpers` (MessageDeduplicator),
  `gateway.platforms._shared` (get_scoped_secret, extra_or_secret,
  seed_extra_from_env, coerce_port, send_error), `gateway.config` (Platform).
  Pattern follows the bundled `plugins/platforms/irc/` adapter.
- `ctx.register_skill()` for the bundled setup skill.
- `setup_fn` wizard uses only the guide's documented public CLI modules:
  `hermes_cli.gateway_setup_wizard` (print_setup_header, confirm_reconfigure,
  prompt_csv), `hermes_cli.setup` (prompt), `hermes_cli.cli_output`
  (print_info/success/warning), `hermes_cli.config` (get_env_value,
  save_env_value).
- No tools, no hooks, no middleware (capabilities block is empty to match).

## Disclosures (rule 13)

- **Network calls**: outbound HTTPS to `api.africastalking.com` only (sending
  SMS). Inbound: Africa's Talking POSTs callbacks to the adapter's local
  webhook server.
- **Listens on a local port**: aiohttp server binds `127.0.0.1:8055` by default
  (`AT_CALLBACK_HOST`/`AT_CALLBACK_PORT`). Public exposure requires the user's
  deliberate tunnel step; `AT_WEBHOOK_SECRET` adds a constant-time `?secret=`
  check (403 without it) so only AT's configured callback URL is honored.
- **Credentials**: `AT_USERNAME` + `AT_API_KEY` via `requires_env`, read only
  through `_shared.get_scoped_secret`/`extra_or_secret` (profile-scoped). No
  file reads, no `os.getenv`, never logged.
- **PII**: every event carries a phone number; logs mask it
  (`+254••••••78`), `pii_safe=True`.
- **Cost**: plugin is free; production SMS/USSD is metered by Africa's Talking
  on the user's own account (README + skill say this). Sandbox testing is free.
  Outbound chunk cap bounds per-reply cost.
- **Long-running processes**: only the webhook server inside the gateway's own
  lifecycle; nothing spawned, no shell-outs, no telemetry, no self-update.
- **Dependencies**: none beyond Hermes core — the AT client is pure stdlib
  (`urllib`); the webhook server uses core's aiohttp.

## Validation

- `hermes plugins validate africastalking --install-deps --json` on Hermes
  v0.22.7 → **`ok: true`, exit 0, 15/15 checks PASS** (manifest, requires_hermes
  '>=0.22', loadable, capability probe "register() ran in isolation", declared
  tools/hooks/middleware match, security scan, no core override).
- Sole warning — expected and inherent: `security scan caution: tunnel_service`
  (SKILL.md + one setup-wizard hint line). The platform is webhook-driven, so a
  user-chosen tunnel is the documented deployment step; it is disclosed in the
  README, the bundled skill, and here. The plugin never installs or runs a
  tunnel itself — it only prints the command for the user.
- 53 offline tests (parsers, chunker, USSD state machine, secret guard incl.
  non-ASCII regression, SMS/USSD dedup, burst-id/retry semantics, standalone
  cron sender, env enablement):
  `python -m unittest discover -s africastalking/tests -t . -v` → `Ran 53 tests … OK`
- Catalog entry passes `scripts/validate_plugin_catalog.py` locally
  (`OK: 1 file(s) valid`).
- Adversarial diff review (fresh context, vs base.py/IRC/guide): verdict
  ship-with-fixes — 1 major (non-ASCII `?secret=` TypeError → 500s) + 5 minor
  findings, ALL fixed and regression-tested before this PR.
- Live sandbox run: pending — free AT sandbox credentials being provisioned;
  SMS round-trip + USSD menu evidence will be added as a PR comment.
