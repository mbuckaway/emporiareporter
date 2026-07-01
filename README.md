# emporiareporter

Own your energy data end-to-end. `emporiareporter` pulls whole-home and
per-circuit usage from an **Emporia Vue** monitor (via the Emporia cloud), prices
it against **Guelph / Ontario Regulated Price Plan** rates, and produces
per-device costs, bill predictions, daily trends, a year-to-date rollup, and a
plan optimizer — served as a local, TrailLens-styled HTML dashboard. Everything
except the initial `pull` runs **fully offline** against a local pricing table.

Built to run on demand on a single Mac — no always-on service.

## Features

- **Usage pull** — every device/channel (Mains, branch circuits, EV charger,
  smart plugs), including today, cached to CSV. Channel *roles* are cached too,
  so offline commands can tell the whole-home Mains total from branch circuits.
- **Cost engine** — per-device / per-bucket commodity cost, plus an optional
  full delivered-bill estimate (Alectra delivery + Ontario Electricity Rebate +
  HST). Global Adjustment is embedded in the RPP commodity price and is not
  double-counted.
- **Plan optimizer** — reprices the same usage under **TOU**, **ULO**, and
  **Tiered** per billing cycle, picks the cheapest, and reports the savings vs
  the plan you're on today.
- **Bill prediction** — day-type-aware projection for the in-progress cycle,
  energy-only and full totals side by side.
- **Daily trends** — per-day table (on/mid/off split), weekday/weekend averages,
  rolling average + trend slope, and matplotlib SVG charts.
- **Year-to-date** — whole-home and per-device cost from Jan 1 through today,
  with an effective-dated monthly rollup, refreshed every run.
- **Local dashboard** — Jinja2 + a standalone Tailwind v4 stylesheet, light/dark
  theme, served over localhost.
- **Offline-first rates** — a local canonical pricing table is used on every
  run; rates are only ever changed through explicit `rates` commands.

## Requirements

- **Python 3.14+**
- An Emporia account (the stock Vue exposes no local API; data comes from the
  Emporia cloud via [`pyemvue`](https://github.com/magico13/PyEmVue)).
- The standalone **Tailwind v4 CLI** at `.bin/tailwindcss` (used to build the
  dashboard stylesheet; download the binary for your platform from the Tailwind
  releases and mark it executable). Not committed to the repo.

## Setup

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Credentials (git-ignored). keys.json holds ONLY your login and is never
# rewritten by the tool; pyemvue caches its tokens separately in
# config/token_cache.json.
cp config/keys.example.json config/keys.json
chmod 600 config/keys.json          # then fill in your Emporia username/password
```

Note: MFA/2FA-enabled Emporia accounts are not supported by pyemvue's SRP flow.

## Auth with Apple

If you created your Emporia account with **Sign in with Apple** (or Google), it
is a *federated* identity: Apple manages the password and **Emporia never stores
one**. That is why the app correctly says the password "cannot be reset" — there
is no Emporia-side password to reset. pyemvue signs in to Emporia's Cognito pool
with an email + password, so that path does not work for an SSO account as-is.

You do **not** need to add any Sign-in-with-Apple / OAuth code — pyemvue already
supports a token-based login, and this tool wires it up. Pick one of two paths.

### Option A — set a native password on the same email (preferred, durable)

Use the Emporia create-account / sign-up flow (website or app) with the **same
email** as your Apple sign-in and set a password, then use `config/keys.json` as
in [Setup](#setup). A native password lets the tool re-authenticate indefinitely.
If the sign-up flow rejects the email, use Option B — or create a separate
email+password account and share your devices to it from the Emporia app.

### Option B — supply Cognito tokens (no password needed)

pyemvue also accepts an `id_token` / `access_token` / `refresh_token` trio, and
`connect()` checks `config/token_cache.json` **before** `config/keys.json`:

```bash
cp config/token_cache.example.json config/token_cache.json
chmod 600 config/token_cache.json
```

1. Sign in with Apple at <https://web.emporiaenergy.com> in a desktop browser.
2. Open DevTools (F12) → **Application/Storage** → **IndexedDB** →
   `com.amplify.awsCognitoAuthPlugin` → `default.store`.
3. Copy the values of the keys ending in `.hostedUi.idToken`,
   `.hostedUi.accessToken`, and `.hostedUi.refreshToken` (each starts with
   `eyJ`) into the three fields of `config/token_cache.json`.

pyemvue rewrites `token_cache.json` as the tokens rotate. Cognito tokens are
finite-lived: if the refresh token is eventually revoked, `connect()` fails with
a clear error and you re-extract the three values. Both `keys.json` and
`token_cache.json` are git-ignored — keep them out of version control.

## Usage

```bash
python -m emporia_hydro <command>
```

| Command | What it does |
| --- | --- |
| `list-devices` | List every discovered Emporia device/channel (proves auth). |
| `pull` | Pull usage from the cloud → CSV cache (+ channel-role cache). |
| `rates show` | Show the stored rows in effect and the active bucket/rate now (offline). |
| `rates set` | Append/replace a price row (e.g. when Nov 1 prices post). |
| `rates import` | Load an external rates file (validated before replacing). |
| `rates update` / `rates check` | Opt-in diff of stored vs a fetched rates file (never fetched per run). |
| `cost` | Per-device TOU/ULO cost for a range, optionally with the full bill. |
| `compare` | TOU vs ULO vs Tiered per billing cycle + cheapest + savings. |
| `predict` | Predicted bill (energy + full) for the current cycle. |
| `trends` | Per-day usage/cost stats. |
| `ytd` | Year-to-date whole-home rollup. |
| `report` | Generate the full HTML dashboard (report + index + YTD + charts). |
| `serve` | Serve the generated dashboard over local HTTP (default 127.0.0.1:8765). |

`--config-dir` is global and precedes the subcommand. Offline commands read the
usage/channel caches (`--csv`, `--channels`); dates are ISO (`--start`/`--end`),
and `--end` is inclusive (today included).

Example:

```bash
python -m emporia_hydro pull --scale 1MIN                       # today, per-minute
python -m emporia_hydro --config-dir config report              # build the dashboard
python -m emporia_hydro --config-dir config serve               # browse it
```

## Configuration

`config/` holds the canonical, editable tables (all offline):

- `rates.json` — TOU/ULO/Tiered commodity prices, hour schedules, seasonal
  thresholds, and the OEB holiday rules.
- `tariff.json` — Alectra delivery adders, the Ontario Electricity Rebate, HST.
- `settings.json` — timezone, current plan, billing-cycle mode, server, output.
- `keys.json` — Emporia credentials (git-ignored; create from the example).

## Development

```bash
source .venv/bin/activate
pytest              # full suite (unit + functional), enforces >=90% coverage
ruff check .        # lint
ruff format .       # format
```

The full-bill adders are estimates until calibrated against a real bill. The
`pyemvue` cloud path is unofficial and can break if Emporia changes its API.

## License

MIT — see [LICENSE](LICENSE).
