# echo-gemini — agent instructions

**This repo is PUBLIC:** https://github.com/RiffRaff4469/echo-gemini

Turning an Amazon Echo Show 5 (1st gen, codename `checkers`) into a self-hosted Gemini
appliance. Python server under `server/`, prompts under `prompts/`, docs under `docs/`.

## Scope — hard boundary
Read and write **only inside this repo**. Do not read from, copy from, or reference
`~/Panoti`, `~/Drivn_Media`, `~/Wavelength`, or any other project. If a task seems to
need something from outside this repo, stop and ask Jaiden.

## Public-repo rules
- Never commit anything from `.env`. Secrets belong in `.env`, which stays untracked.
- Never commit device identifiers, tailnet names, IPs, serial numbers, or the Echo's
  hardware ID. Use placeholders in docs.
- Never commit client names, business material, or anything from Panoti or Drivn.
- Before any `git push`, re-read the diff for the three items above.

## Build & run
- Server: `.venv/Scripts/python.exe server/main.py` with `.env` sourced.
- Tests: `pytest` (config in `pytest.ini`).
- Branch: `main`.
