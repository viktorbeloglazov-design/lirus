"""Claude API — ядро, которое будет отвечать на вопросы сотрудников."""
from __future__ import annotations

from .base import (OK, WARN, CheckContext, CheckResult, ConnectorError, ConnectorType, Field, details_for,
                   http_request, json_or_none)

API = "https://api.anthropic.com/v1"
API_VERSION = "2023-06-01"

MODELS = [
    ("claude-opus-5", "Claude Opus 5 — лучший баланс ума и цены (рекомендуется)"),
    ("claude-sonnet-5", "Claude Sonnet 5 — быстрее и дешевле"),
    ("claude-haiku-4-5", "Claude Haiku 4.5 — самый дешёвый, для простых вопросов"),
    ("claude-fable-5-1", "Claude Fable 5.1 — самый сильный и самый дорогой"),
]

# Цена за миллион токенов, долларов: (вход, выход). Для учёта расходов на этапе 3.
PRICES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5-1": (10.0, 50.0),
}

ACTION_KEY = (
    "Откройте platform.claude.com → «API Keys» → «Create Key», скопируйте ключ (начинается с "
    "sk-ant-) и вставьте сюда. Старый ключ мог быть удалён или отключён."
)
ACTION_REGION = (
    "Anthropic не обслуживает запросы из страны, где стоит сервер. Укажите в разделе «Настройки» "
    "прокси-сервер в поддерживаемой стране (например, в Казахстане, Европе или США) и повторите проверку."
)


def validate(values: dict) -> dict:
    key = values.get("api_key", "")
    if key and not key.startswith("sk-ant-"):
        return {"api_key": "Ключ Claude API начинается с sk-ant-. Проверьте, что скопировали его целиком."}
    if key.startswith("sk-ant-admin"):
        return {"api_key": "Это административный ключ (sk-ant-admin…). Нужен обычный API-ключ."}
    return {}


async def check(values: dict, ctx: CheckContext) -> CheckResult:
    headers = {"x-api-key": values.get("api_key", ""), "anthropic-version": API_VERSION}
    resp = await http_request("GET", API + "/models?limit=100", ctx, service="Claude API",
                              headers=headers, use_proxy=True)
    data = json_or_none(resp) or {}
    err = (data.get("error") or {}) if isinstance(data, dict) else {}
    err_type = err.get("type", "") if isinstance(err, dict) else ""
    err_msg = str(err.get("message", "")) if isinstance(err, dict) else ""
    if resp.status_code == 200:
        available = {m.get("id") for m in data.get("data", []) if isinstance(m, dict)}
        model = values.get("model") or MODELS[0][0]
        limit = values.get("monthly_limit")
        limit_text = f" Лимит расходов: ${limit:g} в месяц." if isinstance(limit, (int, float)) and limit else ""
        if available and model not in available:
            return CheckResult(WARN, f"Ключ верный, но модель {model} для него недоступна.",
                               "Выберите другую модель в этом подключении и сохраните.",
                               "Доступные модели: " + ", ".join(sorted(x for x in available if x)))
        return CheckResult(OK, f"Claude API работает, модель {model} доступна.{limit_text}")
    if resp.status_code == 401:
        raise ConnectorError("Ключ Claude API неверный.", ACTION_KEY, details_for(resp))
    if resp.status_code == 403:
        if "not allowed" in err_msg.lower() or err_type == "forbidden":
            raise ConnectorError("Claude API отклоняет запросы из страны, где находится сервер.",
                                 ACTION_REGION, details_for(resp))
        raise ConnectorError("У ключа Claude API нет прав на эту операцию.",
                             "Проверьте в platform.claude.com, что ключ относится к рабочему пространству "
                             "с доступом к моделям.", details_for(resp))
    if resp.status_code == 429:
        return CheckResult(WARN, "Claude API просит подождать: превышена частота запросов.",
                           "Повторите проверку через минуту.", details_for(resp))
    if resp.status_code == 529:
        return CheckResult(WARN, "Серверы Claude сейчас перегружены.",
                           "Это временно — повторите проверку через несколько минут.", details_for(resp))
    if resp.status_code >= 500:
        return CheckResult(WARN, f"Сбой на стороне Claude API (код {resp.status_code}).",
                           "Повторите проверку через несколько минут.", details_for(resp))
    raise ConnectorError(f"Claude API ответил ошибкой: {err_msg or resp.status_code}.",
                         ACTION_KEY, details_for(resp))


CONNECTOR = ConnectorType(
    key="claude",
    title="Ядро Claude",
    category="Искусственный интеллект",
    description="Claude API — «мозг» платформы: понимает вопросы сотрудников, собирает данные из "
                "подключённых систем и формулирует ответ. Может быть только одно.",
    singleton=True,
    finance=True,
    foreign=True,
    name_placeholder="Ядро Claude",
    fields=[
        Field("api_key", "API-ключ", "secret", required=True, ascii_only=True, placeholder="sk-ant-...",
              hint="Начинается с sk-ant-. Хранится зашифрованным."),
        Field("model", "Модель", "select", default=MODELS[0][0], options=MODELS,
              hint="Можно поменять в любой момент. Opus 5 подходит для большинства задач."),
        Field("monthly_limit", "Лимит расходов в месяц, $", "number", default=100.0,
              hint="Когда расходы за месяц дойдут до лимита, бот перестанет обращаться к Claude "
                   "до начала следующего месяца. Дополнительно задайте лимит в platform.claude.com → "
                   "«Limits»."),
    ],
    validate=validate,
    check=check,
    help_steps=[
        "Зайдите на platform.claude.com (аккаунт Anthropic Console) под почтой компании.",
        "«Billing» — привяжите карту и пополните баланс (оплата по факту использования).",
        "«API Keys» → «Create Key», назовите «Платформа», скопируйте ключ — он показывается один раз.",
        "Если сервер в России — в «Настройках» панели укажите прокси в другой стране: из России "
        "Claude API не отвечает.",
    ],
)
