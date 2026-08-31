from aiogram.fsm.state import State, StatesGroup


class ReviewSteps(StatesGroup):
    ai_comment = State()      # ждём замечание для правки ИИ
    manual_text = State()     # ждём новый текст поста целиком
    reject_reason = State()   # ждём причину отклонения
    schedule_time = State()   # ждём время публикации