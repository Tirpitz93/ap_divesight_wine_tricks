# Agent playbook (keep token use low)

Read this, `wine/README.md` (Findings + backlog) and `./shim list`. Nothing else up front.

## Loop

1. Pick one hypothesis from `wine/README.md` backlog.
2. Create `wine/profiles/<short-name>.toml` with `extends = "debug"` (or `quiet`) and ONE change.
   Use `./shim run <app> <profile> --dry-run` to verify the effective config.
3. Startup-only checks you can run yourself: `./shim run <app> <profile> --timeout 30 --no-prompt --note "..."`.
   Prefer the mock handset (`--mock`, see README) for anything protocol/timing related — no human or
   rebreather needed, and `## mock device` in the summary shows exactly which requests were served.
   Only real-BT questions (rfcomm, radio timing) need the dive computer; then the human: ask them to run
   `./shim run <app> live --note "..."` (captures BT packets) and answer the outcome prompt;
   read `## packet capture` in the summary, not the pcapng.
4. Read results with `./shim show <run>` (60 lines). Only if needed:
   `./shim show <run> --full`, `./shim diff <a> <b>`, or targeted
   `zgrep -m20 'pattern' logs/<app>/<run>/wine.log*` — never cat raw logs.
5. Record: `./shim note <run> --outcome ... "finding"` and add a dated line to Findings in
   `wine/README.md`; tick/add backlog items.

## Rules

- Agents cannot type a sudo password: if `bluetooth.log` shows a failed bind, ask the human to run
  `./shim bt bind` (or install the sudoers rule in README).
- Never edit files inside `logs/` except via `./shim note`.
- Persistent changes (reg/serial/winetricks) must go through profiles + `wine/patches/`, never ad hoc,
  so every run stays reproducible. Add an inverse `.reg` for each behaviour-changing patch.
- Keep `[wine] debug` focused: `warn+all` floods; mute noisy channels (`warn-x11drv,warn-font`).
- `verbose-io` produces very large logs; use with `--timeout` or short manual tests.
- Run references: `latest`, `divesight:latest`, `communicator`, or any unique substring of a run id.
