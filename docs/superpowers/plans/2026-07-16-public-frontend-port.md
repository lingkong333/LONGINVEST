# Public Frontend Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the existing frontend Nginx container on server port 15173 while keeping the API, PostgreSQL, and Redis private.

**Architecture:** Docker Compose publishes frontend port 8080 on all host IPv4 interfaces at port 15173. The existing frontend Nginx continues to serve the application and proxy `/api/` to the private API service; no other port binding changes.

**Tech Stack:** Docker Compose, Nginx, Pytest, PyYAML

---

### Task 1: Publish and verify the frontend port

**Files:**
- Modify: `backend/tests/integration/test_worker_queue_isolation.py`
- Modify: `deploy/compose.yaml`

- [ ] **Step 1: Add the failing port-boundary test**

Add this test beside the existing Compose topology test:

```python
def test_compose_publishes_only_the_frontend_on_public_port() -> None:
    compose_path = Path(__file__).parents[3] / "deploy" / "compose.yaml"
    services = yaml.safe_load(compose_path.read_text(encoding="utf-8"))["services"]

    assert services["frontend"]["ports"] == ["15173:8080"]
    assert services["api"]["ports"] == ["127.0.0.1:18080:8000"]
    assert "ports" not in services["postgres"]
    assert "ports" not in services["redis"]
```

- [ ] **Step 2: Run the test and confirm the old binding fails**

Run: `backend/.venv/Scripts/python.exe -m pytest backend/tests/integration/test_worker_queue_isolation.py -q`

Expected: FAIL because the frontend still binds `127.0.0.1:15173:8080`.

- [ ] **Step 3: Change the frontend host binding**

Change only the frontend port entry in `deploy/compose.yaml`:

```yaml
ports:
  - 15173:8080
```

- [ ] **Step 4: Verify code and Compose configuration**

Run: `backend/.venv/Scripts/python.exe -m pytest backend/tests/integration/test_worker_queue_isolation.py -q`

Expected: PASS.

Run on the server: `docker compose -f deploy/compose.yaml config --quiet`

Expected: exit code 0.

- [ ] **Step 5: Commit and push**

```text
git add backend/tests/integration/test_worker_queue_isolation.py deploy/compose.yaml docs/superpowers/plans/2026-07-16-public-frontend-port.md
git commit -m "deploy: publish frontend port"
git push server main
```

- [ ] **Step 6: Recreate and verify only the frontend service**

Run on the server: `docker compose -f deploy/compose.yaml up -d --no-deps --force-recreate frontend`

Expected: the frontend container is healthy.

Verify `0.0.0.0:15173` is listening, `http://服务器IP:15173` returns HTTP 200, `/api/health/live` returns HTTP 200, and port 18080 remains bound to `127.0.0.1`.
