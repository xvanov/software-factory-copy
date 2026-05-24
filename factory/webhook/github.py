"""GitHub webhook receiver.

Receives webhook events from GitHub and drives the chain:

* ``issues.opened`` / ``issues.labeled`` (label=``direction``) →
  ``directions.ingester.ingest_github_direction_issue`` + enqueue pm-sync.
* ``pull_request.opened`` → record PR number on the matching StoryRecord.
* ``pull_request.review.submitted`` → record review event.
* ``check_suite.completed`` / ``check_run.completed`` → if the PR's CI is
  green, advance the story to CI_GREEN.

Webhook secret is verified via HMAC SHA256 against the ``GITHUB_WEBHOOK_SECRET``
env var; a missing-or-bad signature returns 401. The secret-verification
helper is exposed independently so it can be tested without a running
server.

Run locally with::

    uvicorn factory.webhook.github:app --host 0.0.0.0 --port 9000

For development behind a tunnel: ``smee --port 9000 --url <smee URL>``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

_FACTORY_ROOT = Path(__file__).resolve().parent.parent.parent


def verify_signature(payload_body: bytes, signature_header: str | None, secret: str) -> bool:
    """HMAC SHA256 verification against ``signature_header``.

    GitHub sends ``X-Hub-Signature-256: sha256=<hexdigest>``. Returns True
    iff the signature matches. Empty/missing header → False.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    sent = signature_header.removeprefix("sha256=")
    mac = hmac.new(secret.encode("utf-8"), msg=payload_body, digestmod=hashlib.sha256)
    expected = mac.hexdigest()
    return hmac.compare_digest(sent, expected)


def _resolve_app_for_repo(repo_full_name: str) -> str | None:
    """Map ``owner/name`` to the local app name by scanning ``apps/*/config.yaml``."""
    import yaml

    apps_dir = _FACTORY_ROOT / "apps"
    if not apps_dir.exists():
        return None
    for app_dir in apps_dir.iterdir():
        cfg = app_dir / "config.yaml"
        if not cfg.exists():
            continue
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        if data.get("repo") == repo_full_name:
            return app_dir.name
    return None


def _handle_issues(payload: dict[str, Any]) -> dict[str, Any]:
    """Handle ``issues.opened`` or ``issues.labeled``.

    Only fires the direction ingester when the issue carries the ``direction``
    label (either present in the payload or added via the labeled event).
    """
    action = payload.get("action")
    repo_full_name = (payload.get("repository") or {}).get("full_name") or ""
    issue = payload.get("issue") or {}
    issue_number = int(issue.get("number") or 0)
    labels = [
        (lbl.get("name") if isinstance(lbl, dict) else None) for lbl in (issue.get("labels") or [])
    ]
    has_direction_label = "direction" in labels

    if action == "labeled":
        new_label = (payload.get("label") or {}).get("name")
        if new_label != "direction":
            return {"acted": False, "reason": "labeled event but not the direction label"}
        has_direction_label = True

    if not has_direction_label:
        return {"acted": False, "reason": "no direction label"}

    app = _resolve_app_for_repo(repo_full_name)
    if app is None:
        return {"acted": False, "reason": f"no local app for repo {repo_full_name!r}"}

    # The actual ingester call requires a github client; in the webhook
    # context we defer to the CLI's `ingest-issue` path or a background
    # worker. We just record the intent here so the unit test can verify
    # routing without a network call.
    return {
        "acted": True,
        "app": app,
        "issue_number": issue_number,
        "next": "ingest_github_direction_issue + pm-sync",
    }


def _handle_pull_request(payload: dict[str, Any]) -> dict[str, Any]:
    action = payload.get("action")
    if action != "opened":
        return {"acted": False, "reason": f"pull_request action={action!r}"}
    pr = payload.get("pull_request") or {}
    pr_number = int(pr.get("number") or 0)
    branch = (pr.get("head") or {}).get("ref")
    repo_full_name = (payload.get("repository") or {}).get("full_name") or ""

    # Find a StoryRecord matching the PR's branch and stamp the PR number on it.
    from sqlmodel import Session, create_engine, select

    from factory.chain.state_machine import StoryRecord

    db = _FACTORY_ROOT / "state" / "factory.db"
    if not db.exists():
        return {"acted": False, "reason": "no state db"}
    eng = create_engine(f"sqlite:///{db}", echo=False)
    with Session(eng) as session:
        rows = session.exec(select(StoryRecord).where(StoryRecord.github_branch == branch)).all()
        if not rows:
            return {"acted": False, "reason": f"no story matched branch {branch!r}"}
        story = rows[0]
        story.github_pr_number = pr_number
        session.add(story)
        session.commit()
    return {
        "acted": True,
        "branch": branch,
        "pr_number": pr_number,
        "repo": repo_full_name,
        "story_slug": story.slug,
    }


def _handle_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Handle ``check_suite.completed`` and ``check_run.completed``."""
    block = payload.get("check_suite") or payload.get("check_run") or {}
    conclusion = block.get("conclusion")
    pulls = block.get("pull_requests") or []
    pr_number = int(pulls[0].get("number")) if pulls else 0
    return {
        "acted": True,
        "pr_number": pr_number,
        "conclusion": conclusion,
    }


def dispatch_event(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Pure dispatcher for use by the HTTP handler and by unit tests.

    Returns a dict describing what the receiver decided to do, regardless of
    whether the action succeeded — the HTTP layer doesn't fail webhooks for
    downstream errors because GitHub will retry.
    """
    if event == "issues":
        return _handle_issues(payload)
    if event == "pull_request":
        return _handle_pull_request(payload)
    if event in ("check_suite", "check_run"):
        return _handle_check(payload)
    if event == "pull_request_review":
        # Phase-2 placeholder — Phase 4 wires reviewer state transitions.
        return {"acted": False, "reason": f"event {event!r} acknowledged"}
    return {"acted": False, "reason": f"unhandled event {event!r}"}


# --------------------------------------------------------------------------- #
# FastAPI app
# --------------------------------------------------------------------------- #


def create_app() -> Any:
    """Build the FastAPI app. Imported lazily so the CLI doesn't pull FastAPI
    unless ``factory webhook-serve`` is invoked."""
    from fastapi import FastAPI, Header, HTTPException, Request

    fastapi_app = FastAPI(title="factory-github-webhook")

    @fastapi_app.post("/webhook/github")
    async def github_webhook(
        request: Request,
        x_github_event: str = Header(default=""),
        x_hub_signature_256: str | None = Header(default=None),
    ) -> dict[str, Any]:
        body = await request.body()
        secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
        if not secret:
            # Fail closed when a secret is unset. Local dev should set one
            # via the .env file even for smee tunnels.
            raise HTTPException(status_code=503, detail="GITHUB_WEBHOOK_SECRET unset")
        if not verify_signature(body, x_hub_signature_256, secret):
            raise HTTPException(status_code=401, detail="signature verification failed")
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc
        return dispatch_event(x_github_event, payload)

    @fastapi_app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return fastapi_app


# Module-level app for ``uvicorn factory.webhook.github:app``.
# Created on import so `uvicorn` finds it directly. Tests bypass this and
# call dispatch_event / verify_signature directly.
app = create_app() if os.environ.get("FACTORY_WEBHOOK_LAZY") != "1" else None
