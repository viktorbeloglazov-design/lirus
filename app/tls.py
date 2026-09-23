"""Сертификат для HTTPS: загрузка .pfx или пары .crt + .key через панель."""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtensionOID, NameOID

from . import config, crypto

CERT_PATH = config.TLS_DIR / "cert.pem"
KEY_PATH = config.TLS_DIR / "key.pem"


@dataclass
class CertInfo:
    names: list[str]
    not_after: datetime
    issuer: str
    self_signed: bool

    @property
    def days_left(self) -> int:
        return (self.not_after - datetime.now(timezone.utc)).days

    def covers(self, host: str) -> bool:
        host = host.lower()
        for name in self.names:
            name = name.lower()
            if name == host or (name.startswith("*.") and host.endswith(name[1:]) and host.count(".") == name.count(".")):
                return True
        return False


class CertError(ValueError):
    pass


def _names(cert: x509.Certificate) -> list[str]:
    names: list[str] = []
    try:
        san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
        names += san.get_values_for_type(x509.DNSName)
        names += [str(ip) for ip in san.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        pass
    if not names:
        names += [a.value for a in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)]
    return names


def _info(cert: x509.Certificate) -> CertInfo:
    issuer = ", ".join(a.value for a in cert.issuer.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)) or \
        ", ".join(a.value for a in cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME))
    return CertInfo(_names(cert), cert.not_valid_after_utc, issuer, cert.issuer == cert.subject)


def install_pfx(data: bytes, password: str) -> CertInfo:
    try:
        key, cert, extra = pkcs12.load_key_and_certificates(data, password.encode("utf-8") if password else None)
    except ValueError as exc:
        text = str(exc).lower()
        if "password" in text or "mac" in text or "decrypt" in text:
            raise CertError("Пароль от файла .pfx неверный. Это пароль, который задавали при "
                            "выгрузке сертификата.") from exc
        raise CertError("Файл не похож на сертификат .pfx/.p12. Проверьте, что выбран правильный файл.") from exc
    if key is None or cert is None:
        raise CertError("В файле .pfx нет закрытого ключа. Выгрузите сертификат заново, отметив "
                        "«Да, экспортировать закрытый ключ».")
    _save(cert, list(extra or []), key)
    return _info(cert)


def install_pem(cert_data: bytes, key_data: bytes, password: str = "") -> CertInfo:
    try:
        certs = x509.load_pem_x509_certificates(cert_data)
    except ValueError as exc:
        raise CertError("Файл сертификата не читается. Нужен файл .crt/.pem в текстовом виде "
                        "(начинается с -----BEGIN CERTIFICATE-----).") from exc
    try:
        key = serialization.load_pem_private_key(key_data, password.encode() if password else None)
    except TypeError as exc:
        raise CertError("Закрытый ключ защищён паролем — укажите пароль.") from exc
    except ValueError as exc:
        raise CertError("Файл ключа не читается или пароль неверный. Нужен файл .key "
                        "(начинается с -----BEGIN PRIVATE KEY-----).") from exc
    cert = certs[0]
    if cert.public_key().public_numbers() != key.public_key().public_numbers():
        raise CertError("Ключ не подходит к сертификату — это файлы от разных сертификатов.")
    _save(cert, certs[1:], key)
    return _info(cert)


def _save(cert, chain, key) -> None:
    if cert.not_valid_after_utc < datetime.now(timezone.utc):
        raise CertError(f"Срок действия сертификата истёк {cert.not_valid_after_utc:%d.%m.%Y}. "
                        "Нужен действующий сертификат.")
    config.TLS_DIR.mkdir(parents=True, exist_ok=True)
    pem = cert.public_bytes(serialization.Encoding.PEM) + b"".join(
        c.public_bytes(serialization.Encoding.PEM) for c in chain)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(crypto.tls_key_password()))
    CERT_PATH.write_bytes(pem)
    KEY_PATH.write_bytes(key_pem)


def installed() -> CertInfo | None:
    if not (CERT_PATH.exists() and KEY_PATH.exists()):
        return None
    try:
        return _info(x509.load_pem_x509_certificates(CERT_PATH.read_bytes())[0])
    except ValueError:
        return None


def remove() -> None:
    for p in (CERT_PATH, KEY_PATH):
        if p.exists():
            p.unlink()


def is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False
