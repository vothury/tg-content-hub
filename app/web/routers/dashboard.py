from datetime import datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select

from app.db.enums import PostStatus, PublishJobState
from app.db.models import LLMCall, Post, PostEvent, PublishJob, TargetChannel
from app.db.session import session_scope
from app.redis_client import get_redis
from app.services.settings import Keys, get_setting
from app.services.times import owner_now, owner_tz
from app.web.auth import get_csrf_token, require_auth
from app.web.charts import bar_series
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_auth)])

STUCK_HOURS = 24


@router.get("/")
async def dashboard(request: Request):
    tz = owner_tz()
    now = owner_now()
    day_start = datetime.combine(now.date(), time.min, tzinfo=tz).astimezone(timezone.utc)
    stuck_before = now - timedelta(hours=STUCK_HOURS)
    day = now.date().isoformat()
    redis = get_redis()
    spent_raw = await redis.get(f"guard:llm_cost:{day}")
    spent = float(spent_raw) if spent_raw else 0.0

    days = [(now - timedelta(days=6 - i)).date() for i in range(7)]
    week_start = datetime.combine(days[0], time.min, tzinfo=tz).astimezone(timezone.utc)

    async with session_scope() as session:
        budget_limit = float(await get_setting(session, Keys.MAX_LLM_BUDGET_USD_PER_DAY))

        att = {}
        for st, cnt in (await session.execute(
                select(Post.status, func.count())
                .where(Post.status.in_([
                    PostStatus.AWAITING_REVIEW, PostStatus.DOUBLE_CHECK_REVIEW,
                    PostStatus.NEEDS_MANUAL_REVIEW, PostStatus.NEEDS_MEDIA_REVIEW]))
                .group_by(Post.status))).all():
            att[st.value] = cnt
        stuck = (await session.execute(
            select(func.count()).select_from(Post)
            .where(Post.status.in_([PostStatus.AWAITING_REVIEW, PostStatus.DOUBLE_CHECK_REVIEW]),
                   Post.created_at < stuck_before))).scalar() or 0
        failed_jobs = (await session.execute(
            select(func.count()).select_from(PublishJob)
            .where(PublishJob.state == PublishJobState.FAILED))).scalar() or 0
        deferred_jobs = (await session.execute(
            select(func.count()).select_from(PublishJob)
            .where(PublishJob.state.in_([PublishJobState.QUEUED, PublishJobState.SCHEDULED]),
                   PublishJob.defer_reason.isnot(None)))).scalar() or 0

        pub_today = (await session.execute(
            select(PublishJob.target_channel_id, func.count())
            .where(PublishJob.state == PublishJobState.DONE,
                   PublishJob.published_at >= day_start)
            .group_by(PublishJob.target_channel_id))).all()
        ch_names = {c.id: c.username for c in (
            await session.execute(select(TargetChannel))).scalars().all()}
        pub_today_total = sum(c for _, c in pub_today)
        pub_today_ch = ", ".join(f"@{ch_names.get(cid, '—')}: {c}" for cid, c in pub_today if c)

        # Кандидатов сегодня — из событий классификации (счётчик redis не рос)
        cand_today = (await session.execute(
            select(func.count()).select_from(PostEvent)
            .where(PostEvent.action == "classified",
                   PostEvent.to_status == PostStatus.CANDIDATE.value,
                   PostEvent.created_at >= day_start))).scalar() or 0
        dup_today = (await session.execute(
            select(func.count()).select_from(PostEvent)
            .where(PostEvent.action == "deduplicated",
                   PostEvent.created_at >= day_start))).scalar() or 0
        autopilot_today = (await session.execute(
            select(func.count()).select_from(Post)
            .where(Post.autopilot.is_(True),
                   Post.status == PostStatus.PUBLISHED,
                   Post.id.in_(select(PublishJob.post_id).where(
                       PublishJob.state == PublishJobState.DONE,
                       PublishJob.published_at >= day_start))))).scalar() or 0

        jobs_week = (await session.execute(
            select(PublishJob.published_at).where(
                PublishJob.state == PublishJobState.DONE,
                PublishJob.published_at >= week_start))).scalars().all()
        calls_week = (await session.execute(
            select(LLMCall.created_at, LLMCall.cost_usd).where(
                LLMCall.created_at >= week_start))).all()
        feed_rows = (await session.execute(
            select(PostEvent.post_id, PostEvent.action, PostEvent.created_at, PostEvent.details)
            .where(PostEvent.action.in_([
                "published", "deduplicated", "publish_failed", "llm_failed"]))
            .order_by(PostEvent.id.desc()).limit(10))).all()

    pub_day = {}
    for pat in jobs_week:
        k = pat.astimezone(tz).date()
        pub_day[k] = pub_day.get(k, 0) + 1
    spend_day = {}
    for cat, cost in calls_week:
        k = cat.astimezone(tz).date()
        spend_day[k] = spend_day.get(k, 0.0) + float(cost or 0)
    pub_series, _ = bar_series([(d.strftime("%d.%m"), pub_day.get(d, 0)) for d in days], height=80)
    spend_series, _ = bar_series(
        [(d.strftime("%d.%m"), round(spend_day.get(d, 0.0), 4)) for d in days], height=80)

    attention = [
        {"label": "На ревью", "count": att.get("AWAITING_REVIEW", 0), "url": "/posts?status=AWAITING_REVIEW"},
        {"label": "Двойная проверка", "count": att.get("DOUBLE_CHECK_REVIEW", 0), "url": "/posts?status=DOUBLE_CHECK_REVIEW"},
        {"label": "Ручная проверка", "count": att.get("NEEDS_MANUAL_REVIEW", 0), "url": "/posts?status=NEEDS_MANUAL_REVIEW"},
        {"label": "Проверка медиа", "count": att.get("NEEDS_MEDIA_REVIEW", 0), "url": "/posts?status=NEEDS_MEDIA_REVIEW"},
        {"label": f"Висит > {STUCK_HOURS} ч", "count": stuck, "url": "/posts?status=AWAITING_REVIEW"},
        {"label": "Публикация: failed", "count": failed_jobs, "url": "/queue"},
        {"label": "Публикация: отложена", "count": deferred_jobs, "url": "/queue"},
    ]

    feed = []
    for pid, action, cat, details in feed_rows:
        feed.append({
            "post_id": pid, "action": action,
            "when": cat.astimezone(tz).strftime("%d.%m %H:%M"),
            "note": (details or {}).get("error") or (details or {}).get("reason") or "",
        })

    return templates.TemplateResponse(request, "dashboard.html", {
        "active": "dashboard",
        "csrf_token": get_csrf_token(request),
        "attention": attention,
        "spent": spent, "budget_limit": budget_limit,
        "pub_today_total": pub_today_total, "pub_today_ch": pub_today_ch,
        "cand_today": cand_today, "dup_today": dup_today,
        "autopilot_today": autopilot_today,
        "pub_series": pub_series, "spend_series": spend_series,
        "feed": feed,
    })