# Rona Tools

Optional tool packages for [Rona](https://github.com/Tech-06/Rona-Public-Edition). Rona's backend ships with a small set of core tools built in; everything else (web search, weather, translation, notes, Google Calendar/Contacts/Mail, ...) lives here as a separate, opt-in package so cloning Rona doesn't force every tool's dependencies and API keys onto every install.

## How this repo is used

You normally never clone this repo directly. From a Rona install:

```bash
rona tools install <package_id>
```

pulls just that one package's folder out of this repo (via `git sparse-checkout`, so the rest of the catalog is never downloaded), installs its Python dependencies, asks for any required configuration, runs its health check, and wires its tools into the registry. Rona's own installer offers the same list as a step during setup.

```bash
rona tools available            # everything in the catalog
rona tools list                 # what you have installed
rona tools config <id>          # show or change a package's configuration
rona tools actions <id>         # operations a package exposes
rona tools run <id> <action>    # run one of them
rona tools verify <id>          # re-run a package's health check
rona tools update <id>          # pull in a newer catalog version
rona tools uninstall <id>
```

(`rona tools` is a thin wrapper over the backend's `python -m toolbox.manager`, which still works if you'd rather call it directly from `backend/` — but you shouldn't need to.)

## Catalog

[`index.json`](index.json) at the repo root lists every package. Each one lives in [`packages/<id>/`](packages/):

| id | what it does | needs |
|---|---|---|
| [`get_time`](packages/get_time) | current date/time in any IANA timezone | nothing |
| [`web_search`](packages/web_search) | web search via Tavily | a free [Tavily](https://tavily.com) API key |
| [`weather`](packages/weather) | current weather for a city | a free [OpenWeatherMap](https://openweathermap.org/api) API key |
| [`deepl_translate`](packages/deepl_translate) | text translation | a [DeepL](https://www.deepl.com/pro-api) API key |
| [`notes`](packages/notes) | a local notes list in Rona's own database | nothing |
| [`google_auth`](packages/google_auth) | shared Google OAuth plumbing | a Google Cloud OAuth `credentials.json`; installed automatically as a dependency, provides no tools itself |
| [`google_calendar`](packages/google_calendar) | list/add/edit/delete Calendar events | `google_auth` + at least one authorized account |
| [`google_contacts`](packages/google_contacts) | list/add/edit/delete Contacts | `google_auth` + at least one authorized account |
| [`google_mail`](packages/google_mail) | send mail / read the recent inbox | `google_auth` + at least one authorized account |
| [`blackboard`](packages/blackboard) | courses, announcements, calendar, assignments, grades and course content from Blackboard Learn | a Blackboard Learn account at any institution -- see [Blackboard account](#blackboard-account) below |

### Dependencies

A package declares what it needs in its manifest's `requires`, and `index.json` mirrors that so Rona can tell you what an install involves *before* downloading anything. Installing `google_calendar` therefore warns you that `google_auth` comes with it and asks first; if you agree, `google_auth` is installed and configured to completion before the calendar package is touched. Uninstalling something another package still requires is refused unless you pass `--force`.

A `requires` entry can be a bare package id (`"google_auth"`) or carry an optional pip-like version constraint: `"google_auth>=2.0"`, `"google_auth>=2.0,<3"`, `"x~=2.1"`. Supported operators are `== != >= <= > < ~=`; a comma between specifiers is AND, so `>=2.0,<3` means "2.x, at least 2.0". Versions are plain dotted integers (`2`, `2.0`, `2.1.3`) -- no pre-releases, no wildcards. Both a package's own `manifest.json` and its mirror entry in `index.json` use this same syntax. If an installed dependency stops satisfying a constraint (a dependent package tightened it in a newer version), Rona's registry logs a warning and surfaces it in the dashboard rather than refusing to load anything; running `rona tools update` on the outdated dependency (or letting the dependent's own update pull it in automatically) is what clears it.

> **Compatibility note:** an older Rona install parses a bare `requires` entry as nothing but a package id -- an entry like `"x>=1"` reads as a package literally named `x>=1`, which it then can't find. Don't publish a version constraint for a dependency in `index.json` until installs have had a real chance to update past the version that added constraint support.

## Google accounts

One OAuth client authorizes as many accounts as you like. The account names are yours to choose — `personal`, `work`, whatever — and the set of them is simply whichever ones you have authorized; nothing in this repo has a fixed list.

**One-time setup.** Installing `google_auth` (directly, or as a dependency of one of the three Google packages) asks for a `credentials.json`: Google Cloud Console → APIs & Services → Credentials → OAuth client ID → **Desktop app**. That one file covers every account.

**Adding an account.** From the dashboard, Settings → Connections → Tool Packages → `google_auth` → *Add a Google account*. Or:

```bash
rona tools run google_auth add_account
```

Either way you get a link. Open it on **any device** — it does not have to be the machine Rona runs on — approve access, and the browser will then try to load `http://localhost:47111/…` and show a "site can't be reached" error. That is expected: nothing is listening there, and the address bar is the whole point. Copy that address, paste it back, done.

This is what makes a server install work. The older `add_account.py` (still present, and exposed as the `add_account_here` action) opens a browser on the machine the *backend* runs on and waits for a redirect to that machine's own `localhost` — fine on your own desktop, impossible over SSH.

**Then allow it.** Authorizing an account doesn't yet let any tool use it; each Google package has its own allowed-accounts list, so you can let Calendar see two accounts and Mail only one. Set it from the same dashboard panel, or:

```bash
rona tools config google_calendar --set accounts=personal,work
```

`rona tools run google_auth list_accounts` shows what is authorized and whether each token still works; `remove_account` revokes one.

## Blackboard account

`blackboard` talks to your institution's own Blackboard Learn site (`base_url` in its config -- there's no shared "Blackboard cloud" to point at, every school runs its own). It has to be logged in separately from Rona itself, and there are three ways to do that depending on what your institution's Blackboard requires:

- **`rona tools run blackboard login`** -- signs in directly over HTTP, no browser at all, typically a couple of seconds. This is the right choice on a server. Only works if your institution's Blackboard has a direct login form (no single sign-on in front of it); it falls back to a headless browser automatically for the rare institution whose login page needs one.
- **`rona tools run blackboard login_sso`** -- opens a real, visible browser window on the machine the backend runs on, and waits for you to finish signing in yourself (SSO, MFA, whatever your institution asks for) before saving the session. Only useful on your own machine, not over SSH -- it's `cli_only` for exactly that reason.
- **`rona tools run blackboard import_session`** -- paste in the cookies from a browser where you're already logged in (DevTools -> Network -> any request -> the `Cookie` header). Works anywhere, including over SSH, and needs no browser automation at all.

`rona tools run blackboard session_status` shows whether a session is stored and still valid; `logout` deletes it.

The session is just cookies, the same way any browser session is -- there's no long-lived refresh token like the Google packages get from OAuth. But once a username and password are configured, the package takes care of that itself: it renews the session before it expires and logs back in from scratch if there's no session at all, so a server install keeps working without anyone re-running `login` by hand. Without a username/password on file (an SSO-only account using `import_session`), a fresh login is still on you once the session Blackboard gave you expires.

## Package format

```
packages/<id>/
    manifest.json     # id, version, kind (tool|library), provides, requires,
                      # python_requirements, config fields, actions,
                      # user_data_globs, schema_sql, health_check
    tools.json        # same shape as Rona's own toolbox/tools.json ("tools": [...]);
                      # a leading "." on "module" is relative to this package
    config.json       # written by Rona for non-secret config (git-ignored
                      # in an installed copy; not part of this repo)
    *.py              # the tool implementation(s)
    health.py         # optional; a check(config) -> {"ok": bool, "detail": str}
                      # Rona runs after configuring the package
    actions.py        # optional; operator-facing operations (see below)
```

### Configuration

A `manifest.json`'s `config` entries can target:

- `"env"` — a secret, written into Rona's `.env` under `env_var`
- `"config"` — a non-secret value (e.g. a list of Google account names), stored in the installed package's own `config.json`
- `"file"` — a local file Rona copies in under `dest_filename` (e.g. `google_auth`'s `credentials.json`)

Required fields are asked for at install time. Everything is editable afterwards from `rona tools config <id>` or the dashboard, so prefer `"required": false` for anything a user can't reasonably know before the package is installed.

A tool's `tools.json` schema can reference a config value with `{"$config": "<key>"}` (see `google_calendar/tools.json`'s `account_name` enum) — Rona resolves it from the package's config at load time, and drops the placeholder cleanly if it isn't configured yet.

### Actions

Tools are what the *model* calls. Actions are what a *person* calls: one-off setup and maintenance operations that have no business in the model's tool list. They appear in `rona tools actions/run` and in the dashboard's Tool Packages panel.

```json
"actions": [
  {
    "id": "add_account",
    "label": "Add a Google account",
    "description": "shown next to the button",
    "handler": ".actions:add_account",
    "params": [{"key": "account_name", "label": "Account name", "required": true}],
    "destructive": false,
    "cli_only": false
  }
]
```

`handler` uses the same `"<module>:<function>"` form as `health_check`. `params` are `config` field descriptors, so one form renderer serves both. `destructive` makes the caller confirm first; `cli_only` keeps an action off the web dashboard (for something that blocks far longer than a request, or that only makes sense on the backend's own machine).

A handler is called as `handler(config, params, state)` and returns one of:

```python
{"status": "ok", "message": "...", "data": {...}}
{"status": "error", "message": "..."}
{"status": "input_required", "message": "...", "fields": [...], "state": {...}}
```

`input_required` is how multi-step flows work. The caller shows `message`, collects `fields`, and calls the same action again with those values plus the `state` you returned, handed back unchanged. `state` must be JSON-serialisable — that is exactly what lets the dashboard run the two halves as two separate HTTP requests without Rona holding a session open in between. `google_auth`'s `add_account` is the worked example: step one returns the consent URL, step two takes the pasted redirect.

### User data

`user_data_globs` lists patterns matching files the *user* produced rather than files the package shipped — `google_auth`'s `token_*.json`. Uninstall keeps those by default (parking them aside and restoring them if the package is reinstalled) instead of deleting them, because re-doing that work may not even be possible from the machine at hand. `rona tools uninstall <id> --purge` deletes them.

### Updating a package

`rona tools update <id>` (or the dashboard's Catalog tab) swaps a package's installed folder for a freshly-fetched one at a newer catalog version, then re-runs its `schema_sql` and health check. Preserved automatically, without needing anything in `user_data_globs`: `config.json`, any `.env` entries, and any file placed through a `file`-typed config field (matched by target filename, old and new manifest combined). `user_data_globs` still matters for update, not just uninstall -- add to it any file your *code* writes at runtime that isn't one of those (a cache, a session file, anything not covered by a `config`/`file` field), or an update will silently leave it behind in the old folder instead of carrying it into the new one. `schema_sql` runs again on every update against the same database, so it has to be safe to re-run (`CREATE TABLE IF NOT EXISTS`, not a bare `CREATE TABLE`) -- treat it the same as a migration you might run twice. If anything about the update fails -- pip, the schema, a failed health check without `--keep-on-health-failure`, or the new version simply failing to load -- Rona restores the previous version automatically; nothing is left half-upgraded.

## Adding a new package

1. Add a `packages/<id>/` folder following the format above.
2. Add an entry for it to [`index.json`](index.json), including its `kind` and `requires` so dependency plans stay accurate without downloading it.
3. Open a PR. Keep each package self-contained (its own `.py` files, no reaching into another package except through a declared `requires`) so installing one never silently needs another's internals.

## License

Same as the main Rona repository — see [LICENSE](https://github.com/Tech-06/Rona-Public-Edition/blob/main/LICENSE).
