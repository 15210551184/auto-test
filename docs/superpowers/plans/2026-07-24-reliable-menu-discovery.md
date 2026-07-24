# Reliable Menu Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Discover every menu branch in accordion-style admin UIs while safely ignoring invalid login-state files and reporting progress.

**Architecture:** Extract validation of Playwright storage-state files into `engine/state.py`. Replace global menu expansion with branch-by-branch collection, then reopen each branch while probing its leaf pages. Progress is supplied through the existing crawler callback and therefore reaches the web SSE log unchanged.

**Tech Stack:** Python 3.10, Playwright sync API, `unittest`, Docker Compose.

## Global Constraints

- Invalid, empty, or directory login-state paths must never be passed to Playwright.
- A fresh clone must not file-mount a missing `auth.json` path.
- Discovery output must include useful progress without exposing credentials or storage-state contents.

---

### Task 1: Validate reusable login state

**Files:**
- Create: `autotest/engine/state.py`
- Create: `tests/test_state.py`
- Modify: `autotest/engine/crawler.py`, `autotest/engine/scanner.py`, `autotest/engine/batch.py`, `autotest/engine/runner.py`, `autotest/cli.py`

**Interfaces:**
- Produces: `valid_storage_state(path: Optional[str], on_warning: Optional[Callable[[str], None]] = None) -> Optional[str]`.

- [ ] Write unit tests for missing, directory, empty, malformed, and valid storage-state files.
- [ ] Run `python -m unittest tests.test_state -v` and confirm tests fail before the helper exists.
- [ ] Implement the helper using regular-file and JSON-object validation; use it before every `browser.new_context` call.
- [ ] Run the state tests again and confirm they pass.

### Task 2: Collect accordion menu branches with progress

**Files:**
- Modify: `autotest/engine/crawler.py`
- Create: `tests/test_crawler.py`

**Interfaces:**
- Extends: `crawl_menu(..., on_progress=None)` and forwards callback from `discover`.

- [ ] Write tests for progress callback forwarding and pure menu-item de-duplication helpers.
- [ ] Run `python -m unittest tests.test_crawler -v` and confirm the new behavior fails.
- [ ] Replace all-at-once expansion with a loop that opens one submenu, collects its currently visible leaf entries, and logs branch count; emit probe progress and skip reasons.
- [ ] Run crawler tests and static compilation.

### Task 3: Make Docker startup safe and document migration

**Files:**
- Modify: `autotest/docker-compose.yml`, `autotest/cli.py`, `autotest/README.md`, `autotest/DEPLOY_DOCKER.md`, `autotest/DEPLOY_1PANEL.md`

**Interfaces:**
- Default state path: `auth/state.json`; compose mounts `./auth` at `/app/auth`.

- [ ] Update the default path and use directory mounting rather than a fragile single-file bind mount.
- [ ] Document copying a locally generated state file to `auth/state.json`.
- [ ] Run `docker compose config` and the complete unit-test suite.
