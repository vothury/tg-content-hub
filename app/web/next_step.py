"""Подсказка «следующий шаг» для статуса поста: кто сейчас ходит и почему."""
from __future__ import annotations

from app.config import settings
from app.db.enums import PostStatus


def next_step_hint(post, channel) -> str:
    st = post.status
    if st in (PostStatus.NEW, PostStatus.PREFILTERED, PostStatus.LLM_CLASSIFYING,
              PostStatus.CANDIDATE, PostStatus.REWRITING):
        return "в обработке конвейером (автоматически)"
    if st is PostStatus.AWAITING_REVIEW:
        if channel is None or not channel.autopilot:
            return "ход владельца: автопилот у канала выключен"
        min_score = channel.autopilot_min_score or settings.autopilot_min_score
        if (post.score or 0.0) < min_score:
            return (f"ход владельца: оценка {post.score or 0.0:.1f} ниже порога "
                    f"автопилота {min_score}")
        return "ход владельца: автопилот уже отработал, дальнейшей автоматики нет"
    if st is PostStatus.DOUBLE_CHECK_REVIEW:
        return "ход владельца: двойная проверка не одобрила (см. примечание)"
    if st is PostStatus.NEEDS_MEDIA_REVIEW:
        return "ход владельца: проверить медиа (✅ подходит / отклонить)"
    if st is PostStatus.NEEDS_MANUAL_REVIEW:
        return "ход владельца: повторить обработку или решить вручную"
    if st in (PostStatus.MANUAL_EDITING, PostStatus.REVISION):
        return "ожидает завершения правки черновика"
    if st is PostStatus.APPROVED:
        return "одобрен — ожидает публикации (очередь/лимиты канала)"
    if st is PostStatus.SCHEDULED:
        return "запланирован — ожидает времени публикации"
    if st is PostStatus.FAILED:
        return "публикация не удалась — повтор или решение владельца"
    if st in (PostStatus.PUBLISHED, PostStatus.UNSUITABLE, PostStatus.REJECTED,
              PostStatus.DEDUPLICATED):
        return "терминальный статус — действий не требуется"
    return "—"