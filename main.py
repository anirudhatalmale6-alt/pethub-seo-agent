import json
import os
import logging
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import settings
from seo_auditor import run_full_audit, audit_single_page, fetch_all_pages, fetch_all_posts
from auto_fixer import auto_fix_safe_issues
from manager_client import heartbeat, create_task, update_task, update_kpi, log_message

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("seo-agent")

scheduler = AsyncIOScheduler()

audit_state = {
    "last_audit": None,
    "running": False,
    "history": [],
    "auto_fix_enabled": True,
    "last_fix_result": None,
}


def load_state():
    os.makedirs(os.path.dirname(settings.DB_PATH), exist_ok=True)
    if os.path.exists(settings.DB_PATH):
        try:
            with open(settings.DB_PATH, "r") as f:
                data = json.load(f)
                audit_state["last_audit"] = data.get("last_audit")
                audit_state["history"] = data.get("history", [])
        except Exception:
            pass


def save_state():
    os.makedirs(os.path.dirname(settings.DB_PATH), exist_ok=True)
    with open(settings.DB_PATH, "w") as f:
        json.dump({
            "last_audit": audit_state["last_audit"],
            "history": audit_state["history"][-30:],
        }, f, default=str)


async def send_heartbeat():
    metrics = {}
    if audit_state["last_audit"]:
        la = audit_state["last_audit"]
        metrics = {
            "tasks_completed": len(audit_state["history"]),
            "tasks_failed": 0,
            "avg_latency_ms": 0,
        }
    await heartbeat("active", metrics)


async def run_scheduled_audit():
    if audit_state["running"]:
        logger.info("Audit already running, skipping")
        return

    audit_state["running"] = True
    task = await create_task("Scheduled SEO Audit", "full_audit", priority="normal")
    task_id = task["id"] if task else None

    if task_id:
        await update_task(task_id, "in_progress")

    try:
        await log_message("info", "Starting scheduled SEO audit")
        result = await run_full_audit()

        summary = {k: v for k, v in result.items() if k != "results"}
        summary["top_issues"] = []
        for r in result["results"][:5]:
            summary["top_issues"].append({
                "page": r["title"],
                "url": r["url"],
                "score": r["score"],
                "issues": r["issues"][:3],
            })

        audit_state["last_audit"] = result
        audit_state["history"].append({
            "date": datetime.now(timezone.utc).isoformat(),
            "total_pages": result["total_pages"],
            "average_score": result["average_score"],
            "total_issues": result["total_issues"],
        })
        save_state()

        await update_kpi("seo_compliance_rate", result["average_score"])

        pages_count = result["total_pages"]
        await update_kpi("pages_published_week", pages_count)

        if audit_state["auto_fix_enabled"]:
            await log_message("info", "Running auto-fix on safe issues...")
            fix_result = await auto_fix_safe_issues(result["results"])
            audit_state["last_fix_result"] = fix_result
            summary["auto_fix"] = fix_result
            if fix_result["total_fixes"] > 0:
                await log_message("info", f"Auto-fixed {fix_result['total_fixes']} issues across {fix_result['pages_fixed']} pages")

        if task_id:
            await update_task(task_id, "completed", output_data=summary)

        await log_message("info", f"SEO audit complete: {result['total_pages']} pages, avg score {result['average_score']}%")

    except Exception as e:
        logger.error(f"Audit failed: {e}")
        if task_id:
            await update_task(task_id, "failed", error_message=str(e))
        await log_message("error", f"SEO audit failed: {e}")
    finally:
        audit_state["running"] = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_state()
    scheduler.add_job(send_heartbeat, "interval", seconds=settings.HEARTBEAT_INTERVAL, id="heartbeat")
    scheduler.add_job(run_scheduled_audit, "cron", hour="3", id="daily_audit")
    scheduler.start()
    await send_heartbeat()
    await log_message("info", "SEO Agent started")
    logger.info("SEO Agent started")
    yield
    scheduler.shutdown()


app = FastAPI(
    title="PetHub SEO Agent",
    description="Automated SEO auditing and optimization for pethubonline.com",
    version="1.0.0",
    lifespan=lifespan,
    root_path="/agents/seo"
)


@app.get("/api/status")
async def get_status():
    last = audit_state["last_audit"]
    return {
        "agent": "seo",
        "status": "running" if audit_state["running"] else "idle",
        "last_audit_date": last["audited_at"] if last else None,
        "last_audit_score": last["average_score"] if last else None,
        "total_pages_audited": last["total_pages"] if last else 0,
        "total_issues": last["total_issues"] if last else 0,
        "audit_history": audit_state["history"][-10:],
    }


@app.post("/api/audit/run")
async def trigger_audit():
    if audit_state["running"]:
        raise HTTPException(409, "Audit already running")
    import asyncio
    asyncio.create_task(run_scheduled_audit())
    return {"message": "Audit started", "status": "running"}


@app.get("/api/audit/results")
async def get_audit_results(min_score: int = None, max_score: int = None, has_issues: bool = None):
    if not audit_state["last_audit"]:
        return {"message": "No audit results yet. Trigger an audit first.", "results": []}

    results = audit_state["last_audit"]["results"]

    if min_score is not None:
        results = [r for r in results if r["score"] >= min_score]
    if max_score is not None:
        results = [r for r in results if r["score"] <= max_score]
    if has_issues is not None:
        if has_issues:
            results = [r for r in results if r["issues_count"] > 0]
        else:
            results = [r for r in results if r["issues_count"] == 0]

    return {
        "total": len(results),
        "summary": {k: v for k, v in audit_state["last_audit"].items() if k != "results"},
        "results": [{
            "page_id": r["page_id"],
            "title": r["title"],
            "url": r["url"],
            "score": r["score"],
            "issues_count": r["issues_count"],
            "warnings_count": r["warnings_count"],
            "passes_count": r["passes_count"],
            "issues": r["issues"],
            "warnings": r["warnings"],
            "word_count": r["content"]["word_count"],
            "images_missing_alt": r["content"]["images_missing_alt"],
            "broken_links": len(r["broken_links"]),
        } for r in results],
    }


@app.get("/api/audit/page/{page_id}")
async def get_page_audit(page_id: int):
    if not audit_state["last_audit"]:
        raise HTTPException(404, "No audit results yet")
    for r in audit_state["last_audit"]["results"]:
        if r["page_id"] == page_id:
            return r
    raise HTTPException(404, "Page not found in audit results")


@app.post("/api/audit/page/{page_id}")
async def audit_page_now(page_id: int):
    import base64
    import httpx
    auth = "Basic " + base64.b64encode(f"{settings.WP_USER}:{settings.WP_APP_PASSWORD}".encode()).decode()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{settings.WP_URL}/wp-json/wp/v2/pages/{page_id}",
            headers={"Authorization": auth}
        )
        if resp.status_code != 200:
            resp2 = await client.get(
                f"{settings.WP_URL}/wp-json/wp/v2/posts/{page_id}",
                headers={"Authorization": auth}
            )
            if resp2.status_code != 200:
                raise HTTPException(404, "Page/post not found")
            page = resp2.json()
        else:
            page = resp.json()
    result = await audit_single_page(page)
    return result


@app.get("/api/audit/issues")
async def get_all_issues():
    if not audit_state["last_audit"]:
        return {"issues": [], "total": 0}
    all_issues = []
    for r in audit_state["last_audit"]["results"]:
        for issue in r["issues"]:
            all_issues.append({"page": r["title"], "url": r["url"], "page_id": r["page_id"], "issue": issue, "type": "error"})
        for warning in r["warnings"]:
            all_issues.append({"page": r["title"], "url": r["url"], "page_id": r["page_id"], "issue": warning, "type": "warning"})
    return {"issues": all_issues, "total": len(all_issues)}


@app.get("/api/audit/history")
async def get_audit_history():
    return {"history": audit_state["history"]}


@app.get("/api/autofix/status")
async def autofix_status():
    return {
        "enabled": audit_state["auto_fix_enabled"],
        "last_result": audit_state["last_fix_result"],
    }


@app.post("/api/autofix/toggle")
async def toggle_autofix(enabled: bool = True):
    audit_state["auto_fix_enabled"] = enabled
    return {"enabled": enabled}


@app.post("/api/autofix/run")
async def run_autofix_now():
    if not audit_state["last_audit"]:
        raise HTTPException(400, "No audit results to fix. Run an audit first.")
    fix_result = await auto_fix_safe_issues(audit_state["last_audit"]["results"])
    audit_state["last_fix_result"] = fix_result
    return fix_result


@app.get("/", response_class=HTMLResponse)
async def seo_dashboard():
    with open("templates/seo_dashboard.html", "r") as f:
        return HTMLResponse(f.read())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=settings.API_PORT, reload=False)
