from datetime import datetime, time, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app.db.enums import PublishJobState
from app.db.models import LLMCall, Post, PostEvent, PublishJob, TargetChannel
from app.db.session import session_scope
from app.services.times import owner_now, owner_tz
from app.web.charts import bar_series, stacked_series
from app.web.auth import get_csrf_token, require_auth
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_auth)])


def _snip(text: str) -> str:
    t = " ".join((text or "").split())
    return t[:80] + (".." if len(t) > 80 else "")


def _day_range(date_str: str):
    from datetime import datetime
    from app.services.times import owner_tz
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    tz = owner_tz()
    start = datetime.combine(d, time.min, tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(d, time.max, tzinfo=tz).astimezone(timezone.utc)
    return start, end


async def _queue_rows(page: int = 1, per_page: int = 50,
                      date_from: str = "", date_to: str = ""):
    async with session_scope() as session:
        base = select(PublishJob)
        if date_from:
            try:
                base = base.where(PublishJob.created_at >= _day_range(date_from)[0])
            except ValueError:
                pass
        if date_to:
            try:
                base = base.where(PublishJob.created_at <= _day_range(date_to)[1])
            except ValueError:
                pass
        total = (await session.execute(
            select(func.count()).select_from(base.subquery()))).scalar() or 0
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(max(1, page), pages)
        jobs = (await session.execute(
            base.order_by(PublishJob.id.desc())
            .limit(per_page).offset((page - 1) * per_page))).scalars().all()
        channels = (await session.execute(select(TargetChannel))).scalars().all()
        post_ids = [j.post_id for j in jobs]
        texts = {}
        if post_ids:
            for p in (await session.execute(
                    select(Post).where(Post.id.in_(post_ids)))).scalars().all():
                texts[p.id] = p.original_text or ""
    ch_map = {c.id: c.username for c in channels}
    ch_link = {c.id: (c.username or f"c/{c.telegram_id}") for c in channels}
    rows = [
        {
            "id": j.id, "post_id": j.post_id, "state": j.state.value,
            "mode": j.mode.value, "channel": ch_map.get(j.target_channel_id, "—"),
            "scheduled_at": j.scheduled_at, "published_at": j.published_at,
            "attempts": j.attempts, "note": j.defer_reason or j.last_error or "",
            "text": _snip(texts.get(j.post_id, "")),
            "url": (f"https://t.me/{ch_link[j.target_channel_id]}/{j.published_message_id}"
                    if j.state is PublishJobState.DONE and j.published_message_id
                    and j.target_channel_id in ch_link else ""),
        }
        for j in jobs
    ]
    sig = "|".join(f"{r['id']}:{r['state']}" for r in rows)
    return rows, sig, total, page, pages


@router.get("/queue")
async def queue_page(request: Request, date_from: str = "", date_to: str = "", page: int = 1):
    rows, sig, total, page, pages = await _queue_rows(page, 50, date_from, date_to)
    base_qs = f"date_from={quote(date_from)}&date_to={quote(date_to)}"
    return templates.TemplateResponse(request, "queue.html", {
        "active": "queue", "csrf_token": get_csrf_token(request),
        "rows": rows, "sig": sig,
        "f_date_from": date_from, "f_date_to": date_to,
        "page": page, "pages": pages, "total": total, "base_qs": base_qs,
    })


@router.get("/api/queue")
async def api_queue():
    _, sig = await _queue_rows()
    return JSONResponse({"sig": sig})


@router.get("/stats")
async def stats_page(request: Request, period: int = 7, date_from: str = "", date_to: str = ""):
    tz = owner_tz()
    now = owner_now()
    if date_from and date_to:
        start_day = datetime.strptime(date_from, "%Y-%m-%d").date()
        end_day = datetime.strptime(date_to, "%Y-%m-%d").date()
    else:
        end_day = now.date()
        start_day = end_day - timedelta(days=max(1, period) - 1)
    days = []
    d = start_day
    while d <= end_day:
        days.append(d)
        d += timedelta(days=1)
    start_utc = datetime.combine(start_day, time.min, tzinfo=tz).astimezone(timezone.utc)
    end_utc = datetime.combine(end_day, time.max, tzinfo=tz).astimezone(timezone.utc)

    async with session_scope() as session:
        jobs = (await session.execute(
            select(PublishJob.published_at, PublishJob.target_channel_id)
            .where(PublishJob.state == PublishJobState.DONE,
                   PublishJob.published_at >= start_utc,
                   PublishJob.published_at <= end_utc))).all()
        calls = (await session.execute(
            select(LLMCall.created_at, LLMCall.model, LLMCall.cost_usd,
                   LLMCall.input_tokens, LLMCall.output_tokens)
            .where(LLMCall.created_at >= start_utc, LLMCall.created_at <= end_utc))).all()
        events = (await session.execute(
            select(PostEvent.created_at, PostEvent.action)
            .where(PostEvent.action.in_(["deduplicated", "llm_rejected", "rejected"]),
                   PostEvent.created_at >= start_utc, PostEvent.created_at <= end_utc))).all()
        channels = (await session.execute(select(TargetChannel))).scalars().all()
    ch_map = {c.id: c.username for c in channels}

    def lk(dt):
        return dt.astimezone(tz).date() if dt else None

    pub_day, pub_ch = {}, {}
    for pat, chid in jobs:
        k = lk(pat)
        if k:
            pub_day[k] = pub_day.get(k, 0) + 1
        pub_ch[chid] = pub_ch.get(chid, 0) + 1
    cost_day, req_day, tok_day, day_model_cost = {}, {}, {}, {}
    model_cost, model_calls, model_tok = {}, {}, {}
    for cat, model, cost, it, ot in calls:
        k = lk(cat)
        c = float(cost or 0)
        t = (it or 0) + (ot or 0)
        if k:
            cost_day[k] = cost_day.get(k, 0.0) + c
            req_day[k] = req_day.get(k, 0) + 1
            tok_day[k] = tok_day.get(k, 0) + t
            day_model_cost.setdefault(k, {})
            day_model_cost[k][model] = day_model_cost[k].get(model, 0.0) + c
        model_cost[model] = model_cost.get(model, 0.0) + c
        model_calls[model] = model_calls.get(model, 0) + 1
        model_tok[model] = model_tok.get(model, 0) + t
    dup_day, rej_day = {}, {}
    for eat, action in events:
        k = lk(eat)
        if not k:
            continue
        if action == "deduplicated":
            dup_day[k] = dup_day.get(k, 0) + 1
        else:
            rej_day[k] = rej_day.get(k, 0) + 1

    top_model = max(model_cost, key=model_cost.get) if model_cost else None
    triples = []
    for k in days:
        mc = day_model_cost.get(k, {})
        a = mc.get(top_model, 0.0) if top_model else 0.0
        b = sum(v for m, v in mc.items() if m != top_model)
        triples.append((k.strftime("%d.%m"), round(a, 4), round(b, 4)))

    pub_series, _ = bar_series([(k.strftime("%d.%m"), pub_day.get(k, 0)) for k in days])
    req_series, _ = bar_series([(k.strftime("%d.%m"), req_day.get(k, 0)) for k in days])
    tok_series, _ = bar_series([(k.strftime("%d.%m"), tok_day.get(k, 0)) for k in days])
    dup_series, _ = bar_series([(k.strftime("%d.%m"), dup_day.get(k, 0)) for k in days])
    rej_series, _ = bar_series([(k.strftime("%d.%m"), rej_day.get(k, 0)) for k in days])
    spend_series, _ = stacked_series(triples)

    top_models = sorted(
        [{"model": m, "calls": model_calls[m], "tokens": model_tok[m],
          "cost": round(model_cost[m], 4)} for m in model_cost],
        key=lambda x: x["cost"], reverse=True)
    ch_rows = sorted(
        [{"channel": ch_map.get(cid, "—"), "count": n} for cid, n in pub_ch.items()],
        key=lambda x: x["count"], reverse=True)
    total_cost = round(sum(model_cost.values()), 4)
    total_pub = sum(pub_day.values())
    unit = round(total_cost / total_pub, 4) if total_pub else None

    return templates.TemplateResponse(request, "stats.html", {
        "active": "stats", "csrf_token": get_csrf_token(request),
        "period": period, "date_from": date_from, "date_to": date_to,
        "pub_series": pub_series, "spend_series": spend_series,
        "req_series": req_series, "tok_series": tok_series,
        "dup_series": dup_series, "rej_series": rej_series,
        "top_model": top_model, "top_models": top_models, "ch_rows": ch_rows,
        "total_cost": total_cost, "total_pub": total_pub, "unit": unit,
    })
