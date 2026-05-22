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
from auto_fixer import auto_fix_safe_issues, get_content_freshness, refresh_stale_content
from schema_generator import generate_and_inject_schemas
from internal_linker import suggest_internal_links, auto_add_internal_links
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
    "last_schema_result": None,
    "schema_running": False,
    "last_links_suggestions": None,
    "last_links_result": None,
    "links_running": False,
    "last_freshness": None,
    "freshness_running": False,
}


def load_state():
    os.makedirs(os.path.dirname(settings.DB_PATH), exist_ok=True)
    if os.path.exists(settings.DB_PATH):
        try:
            with open(settings.DB_PATH, "r") as f:
                data = json.load(f)
                audit_state["last_audit"] = data.get("last_audit")
                audit_state["history"] = data.get("history", [])
                audit_state["last_schema_result"] = data.get("last_schema_result")
                audit_state["last_links_suggestions"] = data.get("last_links_suggestions")
                audit_state["last_links_result"] = data.get("last_links_result")
                audit_state["last_freshness"] = data.get("last_freshness")
        except Exception:
            pass


def save_state():
    os.makedirs(os.path.dirname(settings.DB_PATH), exist_ok=True)
    with open(settings.DB_PATH, "w") as f:
        json.dump({
            "last_audit": audit_state["last_audit"],
            "history": audit_state["history"][-30:],
            "last_schema_result": audit_state["last_schema_result"],
            "last_links_suggestions": audit_state["last_links_suggestions"],
            "last_links_result": audit_state["last_links_result"],
            "last_freshness": audit_state["last_freshness"],
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


async def run_scheduled_schema_generation():
    if audit_state["schema_running"]:
        return
    if not audit_state["last_audit"]:
        logger.info("No audit results for schema generation, skipping")
        return

    audit_state["schema_running"] = True
    try:
        await log_message("info", "Starting scheduled schema generation")
        result = await generate_and_inject_schemas(audit_state["last_audit"]["results"])
        audit_state["last_schema_result"] = result
        save_state()
        await log_message("info", f"Schema generation complete: {result['schemas_injected']} injected")
    except Exception as e:
        logger.error(f"Schema generation failed: {e}")
        await log_message("error", f"Schema generation failed: {e}")
    finally:
        audit_state["schema_running"] = False


async def run_scheduled_link_analysis():
    if audit_state["links_running"]:
        return
    if not audit_state["last_audit"]:
        logger.info("No audit results for link analysis, skipping")
        return

    audit_state["links_running"] = True
    try:
        await log_message("info", "Starting scheduled internal link analysis")
        suggestions = await suggest_internal_links(audit_state["last_audit"]["results"])
        audit_state["last_links_suggestions"] = suggestions
        save_state()
        await log_message("info", f"Link analysis complete: {len(suggestions)} suggestions")
    except Exception as e:
        logger.error(f"Link analysis failed: {e}")
        await log_message("error", f"Link analysis failed: {e}")
    finally:
        audit_state["links_running"] = False


async def run_scheduled_freshness_check():
    if audit_state["freshness_running"]:
        return
    if not audit_state["last_audit"]:
        logger.info("No audit results for freshness check, skipping")
        return

    audit_state["freshness_running"] = True
    try:
        await log_message("info", "Starting scheduled content freshness check")
        result = await get_content_freshness(audit_state["last_audit"]["results"])
        audit_state["last_freshness"] = result
        save_state()
        await log_message("info", f"Freshness check complete: {result['stale_count']} stale pages")
    except Exception as e:
        logger.error(f"Freshness check failed: {e}")
        await log_message("error", f"Freshness check failed: {e}")
    finally:
        audit_state["freshness_running"] = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_state()
    scheduler.add_job(send_heartbeat, "interval", seconds=settings.HEARTBEAT_INTERVAL, id="heartbeat")
    scheduler.add_job(run_scheduled_audit, "cron", hour="3", id="daily_audit")
    scheduler.add_job(run_scheduled_schema_generation, "cron", day_of_week="mon,thu", hour="4", id="schema_biweekly")
    scheduler.add_job(run_scheduled_link_analysis, "cron", day_of_week="mon,wed,fri", hour="5", id="links_3x_week")
    scheduler.add_job(run_scheduled_freshness_check, "cron", day_of_week="tue,fri", hour="2", id="freshness_2x_week")
    scheduler.add_job(run_scheduled_competitor_analysis, "cron", day_of_week="wed", hour="6", id="competitor_weekly")
    scheduler.add_job(run_scheduled_readability_check, "cron", day_of_week="tue,sat", hour="4", minute="30", id="readability_2x_week")
    scheduler.start()
    await send_heartbeat()
    await log_message("info", "SEO Agent started")
    logger.info("SEO Agent started")
    yield
    scheduler.shutdown()


app = FastAPI(
    title="PetHub SEO Agent",
    description="Automated SEO auditing and optimization for pethubonline.com",
    version="2.0.0",
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


@app.post("/api/schema/generate")
async def trigger_schema_generation():
    if audit_state["schema_running"]:
        raise HTTPException(409, "Schema generation already running")
    if not audit_state["last_audit"]:
        raise HTTPException(400, "No audit results. Run an audit first.")
    import asyncio
    asyncio.create_task(run_scheduled_schema_generation())
    return {"message": "Schema generation started", "status": "running"}


@app.get("/api/schema/status")
async def get_schema_status():
    return {
        "running": audit_state["schema_running"],
        "last_result": audit_state["last_schema_result"],
    }


@app.post("/api/links/analyze")
async def trigger_link_analysis():
    if audit_state["links_running"]:
        raise HTTPException(409, "Link analysis already running")
    if not audit_state["last_audit"]:
        raise HTTPException(400, "No audit results. Run an audit first.")
    import asyncio
    asyncio.create_task(run_scheduled_link_analysis())
    return {"message": "Link analysis started", "status": "running"}


@app.get("/api/links/suggestions")
async def get_link_suggestions():
    return {
        "running": audit_state["links_running"],
        "suggestions": audit_state["last_links_suggestions"] or [],
        "total": len(audit_state["last_links_suggestions"] or []),
    }


@app.post("/api/links/apply")
async def apply_internal_links():
    if audit_state["links_running"]:
        raise HTTPException(409, "Link operation already running")
    if not audit_state["last_audit"]:
        raise HTTPException(400, "No audit results. Run an audit first.")
    audit_state["links_running"] = True
    try:
        result = await auto_add_internal_links(audit_state["last_audit"]["results"])
        audit_state["last_links_result"] = result
        save_state()
        return result
    finally:
        audit_state["links_running"] = False


@app.get("/api/freshness")
async def get_freshness_report():
    if audit_state["last_freshness"]:
        return {
            "running": audit_state["freshness_running"],
            "report": audit_state["last_freshness"],
        }
    if not audit_state["last_audit"]:
        return {"running": False, "report": None, "message": "No audit results. Run an audit first."}
    result = await get_content_freshness(audit_state["last_audit"]["results"])
    audit_state["last_freshness"] = result
    save_state()
    return {"running": False, "report": result}


@app.post("/api/freshness/refresh")
async def trigger_freshness_refresh():
    if audit_state["freshness_running"]:
        raise HTTPException(409, "Freshness refresh already running")
    if not audit_state["last_audit"]:
        raise HTTPException(400, "No audit results. Run an audit first.")

    audit_state["freshness_running"] = True
    try:
        freshness = await get_content_freshness(audit_state["last_audit"]["results"])
        refreshed = []
        for stale_page in freshness.get("stale_pages", []):
            page_data = None
            for r in audit_state["last_audit"]["results"]:
                if r["page_id"] == stale_page["page_id"]:
                    page_data = r
                    break
            if page_data:
                page_data["modified"] = stale_page.get("last_modified", "")
                result = await refresh_stale_content(page_data)
                if result["fixes"]:
                    refreshed.append({
                        "page_id": stale_page["page_id"],
                        "title": stale_page["title"],
                        "fixes": result["fixes"],
                    })

        audit_state["last_freshness"] = await get_content_freshness(audit_state["last_audit"]["results"])
        save_state()
        return {"refreshed_count": len(refreshed), "details": refreshed}
    finally:
        audit_state["freshness_running"] = False


@app.get("/", response_class=HTMLResponse)
async def seo_dashboard():
    with open("templates/seo_dashboard.html", "r") as f:
        return HTMLResponse(f.read())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=settings.API_PORT, reload=False)




# ── Scheduled upgrade jobs ──────────────────────────────────────────

async def run_scheduled_competitor_analysis():
    """Weekly competitor gap analysis."""
    try:
        from competitor_analyzer import analyze_competitor_gaps
        await log_message("info", "Starting competitor gap analysis")
        pages = []
        if audit_state["last_audit"]:
            for r in audit_state["last_audit"]["results"][:30]:
                pages.append({
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "word_count": r.get("content", {}).get("word_count", 0),
                    "seo_score": r.get("score", 0),
                })
        result = await analyze_competitor_gaps(pages)
        audit_state["last_competitor_analysis"] = result
        save_state()
        await log_message("info", f"Competitor analysis complete: {len(result.get('gaps', []))} gaps found")
    except Exception as e:
        logger.error(f"Competitor analysis failed: {e}")
        await log_message("error", f"Competitor analysis failed: {e}")


async def run_scheduled_readability_check():
    """Check readability of pages with low SEO scores."""
    try:
        from readability_enhancer import enhance_readability
        await log_message("info", "Starting readability analysis")
        results = []
        if audit_state["last_audit"]:
            low_score_pages = [r for r in audit_state["last_audit"]["results"] if r.get("score", 100) < 70]
            for page in low_score_pages[:10]:
                title = page.get("title", "")
                content_html = page.get("content", {}).get("raw_html", "") or page.get("raw_content", "")
                if content_html:
                    analysis = await enhance_readability(title, content_html)
                    analysis["page_id"] = page.get("page_id", 0)
                    analysis["title"] = title
                    results.append(analysis)
        audit_state["last_readability"] = {
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "total_analyzed": len(results),
            "results": results,
        }
        save_state()
        await log_message("info", f"Readability analysis complete: {len(results)} pages analyzed")
    except Exception as e:
        logger.error(f"Readability check failed: {e}")
        await log_message("error", f"Readability check failed: {e}")


# ── AI-Powered Upgrade Endpoints ─────────────────────────────────────

@app.post("/api/competitor/analyze")
async def analyze_competitors():
    """Run AI competitor gap analysis."""
    from competitor_analyzer import analyze_competitor_gaps
    pages = []
    if audit_state.get("last_audit"):
        for r in audit_state["last_audit"]["results"][:30]:
            pages.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "word_count": r.get("content", {}).get("word_count", 0),
                "seo_score": r.get("score", 0),
            })
    result = await analyze_competitor_gaps(pages)
    audit_state["last_competitor_analysis"] = result
    save_state()
    return result


@app.get("/api/competitor/results")
async def get_competitor_results():
    """Get last competitor analysis results."""
    return audit_state.get("last_competitor_analysis", {"message": "No competitor analysis yet. Run /api/competitor/analyze first."})


@app.post("/api/readability/analyze")
async def analyze_readability(page_id: int):
    """Analyze readability of a specific page."""
    from readability_enhancer import enhance_readability
    import base64
    import httpx
    auth = "Basic " + base64.b64encode(f"{settings.WP_USER}:{settings.WP_APP_PASSWORD}".encode()).decode()
    async with httpx.AsyncClient(timeout=15) as client:
        for endpoint in ["pages", "posts"]:
            r = await client.get(
                f"{settings.WP_URL}/wp-json/wp/v2/{endpoint}/{page_id}",
                headers={"Authorization": auth},
            )
            if r.status_code == 200:
                data = r.json()
                title = data.get("title", {}).get("rendered", "")
                content = data.get("content", {}).get("rendered", "")
                return await enhance_readability(title, content)
    raise HTTPException(404, "Page not found")


@app.get("/api/readability/results")
async def get_readability_results():
    """Get last batch readability analysis results."""
    return audit_state.get("last_readability", {"message": "No readability data yet."})
