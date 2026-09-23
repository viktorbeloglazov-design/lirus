"""Google: ключи приложения (OAuth-клиент) и аккаунты Gmail + Google Диск."""
from __future__ import annotations

import re
from urllib.parse import urlencode

from .base import (ERROR, OK, WARN, CheckContext, CheckResult, ConnectorError, ConnectorType, Field,
                   details_for, http_request, json_or_none)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
GMAIL_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
DRIVE_ABOUT_URL = "https://www.googleapis.com/drive/v3/about?fields=user(emailAddress),storageQuota"

SCOPE_GMAIL = "https://www.googleapis.com/auth/gmail.modify"
SCOPE_DRIVE = "https://www.googleapis.com/auth/drive"
SCOPE_EMAIL = "https://www.googleapis.com/auth/userinfo.email"
SCOPES = [SCOPE_GMAIL, SCOPE_DRIVE, SCOPE_EMAIL]
CALLBACK_PATH = "/oauth/google/callback"

SERVICE = "Google"

ACTION_RECONNECT = (
    "Откройте это подключение и нажмите «Переподключить аккаунт Google». Если такое повторяется "
    "примерно раз в неделю — приложение в Google Cloud находится в режиме «Testing»: откройте "
    "«Google Auth Platform → Audience» и нажмите «Publish app»."
)


def _gb(value) -> str:
    try:
        return f"{int(value) / 1024 ** 3:.1f}".replace(".", ",") + " ГБ"
    except (TypeError, ValueError):
        return "?"


# ---------------------------------------------------------------- приложение

def validate_app(values: dict) -> dict:
    cid = values.get("client_id", "")
    if cid and not cid.endswith(".apps.googleusercontent.com"):
        return {"client_id": "Client ID заканчивается на .apps.googleusercontent.com — проверьте, "
                             "что скопировали его целиком."}
    secret = values.get("client_secret", "")
    if secret and secret.endswith(".apps.googleusercontent.com"):
        return {"client_secret": "Сюда вставлен Client ID, а нужен Client secret (он короче и обычно "
                                 "начинается с GOCSPX-)."}
    return {}


async def check_app(values: dict, ctx: CheckContext) -> CheckResult:
    """Проверяем ключи обменом заведомо неверного кода: Google скажет, знает ли он клиента."""
    resp = await http_request(
        "POST", TOKEN_URL, ctx, service=SERVICE, use_proxy=True,
        data={"client_id": values.get("client_id", ""), "client_secret": values.get("client_secret", ""),
              "code": "proverka-svyazi", "grant_type": "authorization_code",
              "redirect_uri": "http://localhost" + CALLBACK_PATH},
    )
    data = json_or_none(resp) or {}
    error = data.get("error") if isinstance(data, dict) else None
    if error in ("invalid_grant", "redirect_uri_mismatch"):
        return CheckResult(OK, "Ключи приложения Google верные. Теперь можно подключать аккаунты Gmail и Диска.")
    if error in ("invalid_client", "unauthorized_client"):
        raise ConnectorError(
            "Google не узнал приложение: Client ID или Client secret неверные.",
            "Откройте Google Cloud → «APIs & Services → Credentials», нажмите на OAuth-клиент и "
            "скопируйте Client ID и Client secret заново. Если секрет не виден — создайте новый "
            "(«Add secret»).", details_for(resp))
    if error == "deleted_client":
        raise ConnectorError("Этот OAuth-клиент удалён в Google Cloud.",
                             "Создайте новый OAuth-клиент (см. «Где взять данные») и вставьте его ключи.",
                             details_for(resp))
    raise ConnectorError(f"Google ответил неожиданно (код {resp.status_code}).",
                         "Повторите проверку через минуту.", details_for(resp), status=WARN)


APP = ConnectorType(
    key="google_app",
    title="Google: ключи приложения",
    category="Почта и файлы",
    description="Ключи OAuth-приложения из Google Cloud. Нужны один раз — после этого аккаунты Gmail "
                "и Google Диска подключаются кнопкой «Подключить аккаунт Google».",
    singleton=True,
    foreign=True,
    name_placeholder="Google",
    fields=[
        Field("client_id", "Client ID", required=True, ascii_only=True,
              placeholder="1234567890-abc...apps.googleusercontent.com",
              hint="Длинная строка, заканчивается на .apps.googleusercontent.com."),
        Field("client_secret", "Client secret", "secret", required=True, ascii_only=True,
              placeholder="GOCSPX-...", hint="Обычно начинается с GOCSPX-."),
    ],
    validate=validate_app,
    check=check_app,
    help_steps=[
        "Откройте console.cloud.google.com под Google-аккаунтом компании и создайте проект "
        "(вверху «Select a project → New project»), например «Платформа».",
        "«APIs & Services → Library»: найдите и включите (Enable) Gmail API и Google Drive API.",
        "«Google Auth Platform» (раньше «OAuth consent screen»): тип External, название приложения, "
        "ваша почта. В разделе «Audience» нажмите «Publish app» — иначе вход будет слетать раз в неделю.",
        "«Clients» (или «Credentials → Create credentials → OAuth client ID»): тип Web application.",
        "В «Authorized redirect URIs» добавьте адреса, показанные ниже в блоке «Адреса для Google», "
        "и нажмите Create.",
        "Скопируйте Client ID и Client secret в эту форму.",
    ],
)


# ------------------------------------------------------------------ аккаунт

def authorize_url(client_id: str, redirect_uri: str, state: str, login_hint: str = "") -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    if login_hint:
        params["login_hint"] = login_hint
    return AUTH_URL + "?" + urlencode(params)


async def exchange_code(app_values: dict, code: str, redirect_uri: str, ctx: CheckContext) -> dict:
    """Меняет код из Google на токены и узнаёт адрес почты. Ошибки — по-русски."""
    resp = await http_request(
        "POST", TOKEN_URL, ctx, service=SERVICE, use_proxy=True,
        data={"client_id": app_values.get("client_id", ""), "client_secret": app_values.get("client_secret", ""),
              "code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri},
    )
    data = json_or_none(resp) or {}
    if resp.status_code != 200 or "access_token" not in data:
        error = data.get("error") if isinstance(data, dict) else ""
        if error == "redirect_uri_mismatch":
            raise ConnectorError(
                "Google отклонил адрес возврата в панель.",
                f"Добавьте адрес {redirect_uri} в Google Cloud → OAuth-клиент → «Authorized redirect "
                "URIs», подождите 5 минут и попробуйте снова.", details_for(resp))
        if error == "invalid_client":
            raise ConnectorError("Ключи приложения Google неверные.",
                                 "Проверьте подключение «Google: ключи приложения».", details_for(resp))
        raise ConnectorError("Google не выдал доступ (код подтверждения устарел или уже использован).",
                             "Нажмите «Подключить аккаунт Google» ещё раз.", details_for(resp))
    granted = set((data.get("scope") or "").split())
    missing = [s for s in (SCOPE_GMAIL, SCOPE_DRIVE) if granted and s not in granted]
    if not data.get("refresh_token"):
        raise ConnectorError(
            "Google не выдал постоянный доступ (refresh token).",
            "Откройте myaccount.google.com/permissions, удалите доступ для этого приложения и "
            "подключите аккаунт заново.", details_for(resp))
    info = await http_request("GET", USERINFO_URL, ctx, service=SERVICE, use_proxy=True,
                              headers={"Authorization": "Bearer " + data["access_token"]})
    email = (json_or_none(info) or {}).get("email", "") if info.status_code == 200 else ""
    return {"refresh_token": data["refresh_token"], "email": email, "missing_scopes": missing}


async def _access_token(values: dict, app_values: dict, ctx: CheckContext) -> str:
    resp = await http_request(
        "POST", TOKEN_URL, ctx, service=SERVICE, use_proxy=True,
        data={"client_id": app_values.get("client_id", ""), "client_secret": app_values.get("client_secret", ""),
              "refresh_token": values.get("refresh_token", ""), "grant_type": "refresh_token"},
    )
    data = json_or_none(resp) or {}
    if resp.status_code == 200 and data.get("access_token"):
        return data["access_token"]
    error = data.get("error") if isinstance(data, dict) else ""
    if error == "invalid_grant":
        raise ConnectorError("Доступ к аккаунту Google отозван или устарел.", ACTION_RECONNECT, details_for(resp))
    if error in ("invalid_client", "unauthorized_client", "deleted_client"):
        raise ConnectorError("Ключи приложения Google изменились или удалены.",
                             "Проверьте подключение «Google: ключи приложения», затем переподключите аккаунт.",
                             details_for(resp))
    raise ConnectorError(f"Google не выдал доступ (код {resp.status_code}).",
                         "Повторите проверку через минуту.", details_for(resp))


def _api_problem(resp, api_title: str, library_id: str) -> CheckResult:
    body = resp.text.lower() if resp is not None else ""
    if resp.status_code == 403 and ("accessnotconfigured" in body or "service_disabled" in body
                                    or "has not been used" in body):
        return CheckResult(
            ERROR, f"{api_title} не включён в проекте Google Cloud.",
            f"Откройте console.cloud.google.com/apis/library/{library_id}, выберите свой проект и "
            "нажмите «Enable». Через пару минут повторите проверку.", details_for(resp))
    if resp.status_code == 403 and ("insufficient" in body or "scope" in body):
        return CheckResult(
            ERROR, f"При подключении не разрешён доступ к {api_title}.",
            "Нажмите «Переподключить аккаунт Google» и на экране Google отметьте все галочки.",
            details_for(resp))
    if resp.status_code == 401:
        return CheckResult(ERROR, "Google не принял доступ.", ACTION_RECONNECT, details_for(resp))
    return CheckResult(WARN, f"{api_title} ответил неожиданно (код {resp.status_code}).",
                       "Повторите проверку через минуту.", details_for(resp))


def make_account_check(get_app_values):
    async def check_account(values: dict, ctx: CheckContext) -> CheckResult:
        app_values = get_app_values()
        if not app_values:
            raise ConnectorError("Не заполнены ключи приложения Google.",
                                 "Сначала создайте подключение «Google: ключи приложения».")
        if not values.get("refresh_token"):
            raise ConnectorError("Аккаунт ещё не подключён.",
                                 "Откройте подключение и нажмите «Подключить аккаунт Google».")
        token = await _access_token(values, app_values, ctx)
        headers = {"Authorization": "Bearer " + token}
        results = []
        gmail = await http_request("GET", GMAIL_PROFILE_URL, ctx, service="Gmail", use_proxy=True, headers=headers)
        if gmail.status_code == 200:
            total = (json_or_none(gmail) or {}).get("messagesTotal", "?")
            results.append(CheckResult(OK, f"Почта: писем — {total}."))
        else:
            results.append(_api_problem(gmail, "Gmail API", "gmail.googleapis.com"))
        drive = await http_request("GET", DRIVE_ABOUT_URL, ctx, service="Google Диск", use_proxy=True,
                                   headers=headers)
        if drive.status_code == 200:
            quota = (json_or_none(drive) or {}).get("storageQuota", {})
            limit = quota.get("limit")
            used = _gb(quota.get("usage"))
            results.append(CheckResult(OK, f"Диск: занято {used}" + (f" из {_gb(limit)}." if limit else ".")))
        else:
            results.append(_api_problem(drive, "Google Drive API", "drive.googleapis.com"))
        status = max((r.status for r in results), key={OK: 0, WARN: 1, ERROR: 2}.get)
        email = values.get("email", "")
        prefix = f"{email}: " if email else ""
        return CheckResult(status, prefix + " ".join(r.message for r in results),
                           " ".join(r.action for r in results if r.action),
                           " | ".join(r.details for r in results if r.details))

    return check_account


def account_type(get_app_values) -> ConnectorType:
    return ConnectorType(
        key="google_account",
        title="Gmail и Google Диск",
        category="Почта и файлы",
        description="Один аккаунт Google: его почта и Диск. Каждый аккаунт подключается отдельно, "
                    "входом через Google — пароль от почты в панель не вводится.",
        foreign=True,
        oauth=True,
        name_placeholder="Например: Почта отдела продаж",
        name_hint="Необязательно. Если оставить пустым, будет адрес почты.",
        fields=[
            Field("email", "Адрес почты", in_form=False),
            Field("refresh_token", "Доступ Google", "secret", in_form=False),
        ],
        check=make_account_check(get_app_values),
        help_steps=[
            "Сначала один раз заполните подключение «Google: ключи приложения».",
            "Нажмите «Подключить аккаунт Google», выберите нужный аккаунт и отметьте все галочки доступа.",
            "Если Google пишет «Приложение не проверено» — нажмите «Дополнительно → Перейти на страницу» "
            "(это ваше собственное приложение).",
            "Google разрешает вход только на адрес с доменом и https или на localhost. Если внешнего "
            "домена ещё нет — подключайте аккаунты, открыв панель на самом сервере: http://localhost:8080.",
            "Для каждого следующего аккаунта нажмите «Добавить» и повторите.",
        ],
    )


def is_ip_host(host: str) -> bool:
    return bool(re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host or ""))
