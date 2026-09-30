"""Поддельный веб-сервер 1С для проверки коннектора.

    /ka/                         — опубликованная база (тонкий клиент)
    /ka/odata/standard.odata/    — OData, Basic-авторизация в UTF-8
    /ka/hs/platform/ping         — HTTP-сервис расширения
    /noodata/                    — база опубликована, OData — нет

Логин «Робот-интеграция», пароль «пароль-123».
Запуск: python tests/fake_1c.py 8091 [https]
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

USER, PASSWORD = "Робот-интеграция", "пароль-123"


def authorized(request: Request) -> bool:
    header = request.headers.get("authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        user, _, pwd = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return user == USER and pwd == PASSWORD


async def base(request: Request):
    return HTMLResponse("<html><body>1C:Enterprise thin client</body></html>")


async def odata(request: Request):
    if not authorized(request):
        return PlainTextResponse("Unauthorized", status_code=401, headers={"WWW-Authenticate": "Basic"})
    return JSONResponse({"odata.metadata": "x", "value": [
        {"name": "Catalog_Номенклатура", "url": "Catalog_Номенклатура"},
        {"name": "Catalog_Контрагенты", "url": "Catalog_Контрагенты"},
        {"name": "Document_ЗаказКлиента", "url": "Document_ЗаказКлиента"},
        {"name": "AccumulationRegister_ТоварыНаСкладах", "url": "AccumulationRegister_ТоварыНаСкладах"},
    ]})


NOMENKLATURA = [{"Ref_Key": f"00000000-0000-0000-0000-{i:012d}", "Description": f"Товар {i}",
                 "Артикул": f"АРТ-{i:04d}", "DeletionMark": i % 50 == 0} for i in range(1, 1201)]
DATA = {
    "Catalog_Номенклатура": NOMENKLATURA,
    "Catalog_Контрагенты": [{"Ref_Key": "c1", "Description": "ООО «Ромашка»", "ИНН": "7701234567"},
                            {"Ref_Key": "c2", "Description": "ИП Иванов", "ИНН": "500100732259"}],
    "Document_ЗаказКлиента": [{"Ref_Key": "d1", "Number": "КА-000001", "Date": "2026-09-01T10:00:00",
                               "СуммаДокумента": 150000, "Контрагент_Key": "c1"}],
    "AccumulationRegister_ТоварыНаСкладах/Balance()": [
        {"Номенклатура_Key": "00000000-0000-0000-0000-000000000001", "Склад_Key": "s1", "ВНаличииBalance": 12},
        {"Номенклатура_Key": "00000000-0000-0000-0000-000000000002", "Склад_Key": "s1", "ВНаличииBalance": 0}],
}


def _odata_error(status: int, text: str):
    return JSONResponse({"odata.error": {"code": "-1", "message": {"lang": "ru", "value": text}}}, status_code=status)


async def entity(request: Request):
    if not authorized(request):
        return PlainTextResponse("Unauthorized", status_code=401)
    name = request.path_params["name"]
    rows = DATA.get(name)
    if rows is None:
        return _odata_error(404, f"Не найден объект {name}")
    flt = request.query_params.get("$filter", "")
    if flt:
        if flt == "DeletionMark eq false":
            rows = [r for r in rows if not r.get("DeletionMark")]
        elif flt.startswith("Артикул eq '"):
            want = flt.split("'")[1]
            rows = [r for r in rows if r.get("Артикул") == want]
        else:
            return _odata_error(400, "Ошибка разбора выражения отбора")
    skip = int(request.query_params.get("$skip", "0") or 0)
    top = int(request.query_params.get("$top", "1000") or 1000)
    page = rows[skip:skip + top]
    sel = [x for x in request.query_params.get("$select", "").split(",") if x]
    if sel:
        page = [{k: r.get(k) for k in sel} for r in page]
    return JSONResponse({"odata.metadata": "x", "value": page})


async def hs(request: Request):
    if not authorized(request):
        return PlainTextResponse("Unauthorized", status_code=401)
    return JSONResponse({"ok": True})


async def not_found(request: Request):
    return PlainTextResponse("Not found", status_code=404)


app = Starlette(routes=[
    Route("/ka/", base), Route("/ka/odata/standard.odata/", odata), Route("/ka/hs/platform/ping", hs),
    Route("/ka/odata/standard.odata/{name:path}", entity),
    Route("/noodata/", base), Route("/noodata/odata/standard.odata/", not_found),
])

CERT_DIR = Path(__file__).parent / "_certs"


def ensure_certs() -> None:
    """Самоподписанный сертификат на 127.0.0.1 и .pfx с русским паролем «пароль» — только для тестов."""
    if (CERT_DIR / "panel.pfx").exists():
        return
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID

    CERT_DIR.mkdir(exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                                                        x509.DNSName("localhost")]), False)
            .sign(key, hashes.SHA256()))
    (CERT_DIR / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (CERT_DIR / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                         serialization.NoEncryption()))
    (CERT_DIR / "panel.pfx").write_bytes(pkcs12.serialize_key_and_certificates(
        b"panel", key, cert, None, serialization.BestAvailableEncryption("пароль".encode())))


if __name__ == "__main__":
    ensure_certs()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8091
    kwargs = {}
    if len(sys.argv) > 2 and sys.argv[2] == "https":
        kwargs = {"ssl_certfile": str(CERT_DIR / "cert.pem"), "ssl_keyfile": str(CERT_DIR / "key.pem")}
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", **kwargs)
