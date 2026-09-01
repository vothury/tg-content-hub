from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select

from app.db.models import Post, PublishJob
from app.db.session import session_scope
from app.redis_client import get_redis
from app.services.settings import Keys, get_setting
from app.services.times import owner_now
from app.web.auth import get_csrf_token, require_auth
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/")
async def dashboard(request: Request):
    day = owner_now().date().isoformat()
    redis = get_redis()
    spent_raw = await redis.get(f"guard:llm_cost:{day}")
    candidates_raw = await redis.get(f"guard:candidates:{day}")
    spent = float(spent_raw) if spent_raw else 0.0
    candidates = int(candidates_raw) if candidates_raw else 0

    async with session_scope() as session:
        budget_limit = float(await get_setting(session, Keys.MAX_LLM_BUDGET_USD_PER_DAY))
        cand_limit = int(await get_setting(session, Keys.MAX_CANDIDATES_PER_DAY))

        status_rows = (await session.execute(
            select(Post.status, func.count()).group_by(Post.status)
        )).all()
        post_counts = {st.value: cnt for st, cnt in status_rows}
        total_posts = sum(post_counts.values())

        job_rows = (await session.execute(
            select(PublishJob.state, func.count()).group_by(PublishJob.state)
        )).all()
        job_counts = {st.value: cnt for st, cnt in job_rows}

    return templates.TemplateResponse(request, "dashboard.html", {
        "active": "dashboard",
        "csrf_token": get_csrf_token(request),
        "spent": spent,
        "budget_limit": budget_limit,
        "candidates": candidates,
        "cand_limit": cand_limit,
        "post_counts": post_counts,
        "total_posts": total_posts,
        "job_counts": job_counts,
    })