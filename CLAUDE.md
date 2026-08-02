# CLAUDE.md

Guidance for Claude Code in this repo. Keep this file under 40 lines — details belong in `ai-docs/`, not here.

## What this project is

Hevy2Garmin Lite: enriches a Garmin watch activity **in place** with Hevy's exercise names, sets, reps, and weights — no FIT files, no deleting the activity, nothing the watch computed (HR, Training Effect, EPOC, VO2max, calories) is touched. Assumes the user always wears their watch; a Hevy-only workout with no matching watch activity is simply not synced.

## Guardrails

- **Strict TDD.** Before modifying or creating any implementation code: write a failing test, run it to confirm the failure, then write the minimum code to turn it green.
- **Before touching `sync.py`, `mapping.py`, `push.py`, or `garmin_client.py`**, read @ai-docs/architecture.md first — it documents two real bugs in this exact area (exception-type normalization, missing `reraise=True`) that will silently reappear if the pattern isn't followed.
- **For any exact fact** (config keys, HTTP routes, the `exerciseSets` payload shape, DB schema, FIT category enum, function signatures) — grep @ai-docs/RAG.md instead of reading source or guessing.
- **Before starting a new task**, read `ai-docs/implementation_plan.md` and `ai-docs/RAG.md` first to establish context. **Before finishing one**, update `ai-docs/implementation_plan.md`, `CLAUDE.md`, and `ai-docs/RAG.md` with what changed.
- Everything runs in Docker. **No local Python environment** — never `pip install` or run `python` on the host.
- Never run anything on the host, except docker.
- Linter: `ruff` (config in `pyproject.toml`), run via `docker compose run --rm lint`. It only bind-mounts `tests/` — after `ruff --fix` touches `src/`/`scripts/`, use `docker run` with those dirs mounted too, or the fix is lost when the container exits (see Commands).
- CI (`.github/workflows/`): `test.yml` runs lint+test on every push/PR to `main`; `publish.yml` builds and pushes to GHCR on a successful `main` run or a GitHub release.
- `Dockerfile` is multi-stage: `base` (prod deps only — `app`/`spike`/`reauth`) and `dev` (`base` + pytest/ruff/pytest-xdist — `test`/`lint`). `publish.yml` always targets `base`; adding a prod dependency goes in `requirements.txt`, a test/lint-only one in `requirements-dev.txt`.

## Commands

```bash
docker compose build              # build all service images
docker compose up -d app          # run the app (dashboard on :8000, from .env PORT)
docker compose down                # stop it
docker compose run --rm test       # full pytest suite, parallel (pytest-xdist -n auto)
docker compose run --rm lint       # ruff check (src/scripts baked in — see note above for --fix)
docker compose run --rm spike      # Phase G live validation push — real account write, requires typed 'yes'
docker compose run --rm reauth     # manual credential bootstrap (rarely needed — app self-heals)

# single test file / single test:
docker compose run --rm --entrypoint python test -m pytest tests/test_matcher.py -v
docker compose run --rm --entrypoint python test -m pytest tests/test_matcher.py::test_exact_overlap_matches -v
```

**Rebuild not picking up a change?** Don't trust `docker compose build <service>` alone — this repo hit a real stale-`COPY src/` layer bug. Use `docker compose build --no-cache <service>`. Each service (`app`/`test`/`spike`/`reauth`/`lint`) has its own image; rebuilding one does not rebuild the others.
