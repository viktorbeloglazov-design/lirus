"""Собирает архивы для установки: программа + библиотеки, чтобы ставить без интернета.

    python tools/build_release.py            — оба архива
    python tools/build_release.py mac        — только для Mac

Результат: dist/Platforma-<версия>-windows.zip и dist/Platforma-<версия>-mac.zip. В архив не попадают data, logs, backups, venv, тесты
и ключи — только то, что есть в git, плюс папка wheels.
"""
from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import config  # noqa: E402

BUILD = ROOT / "build"
SKIP_PREFIXES = ("tests/", "tools/", "docs/screenshots/", ".git")
PY_VERSIONS = ("3.11", "3.12", "3.13")
# Для каждой системы: какие платформы библиотек качать, какие файлы запуска класть и какие — нет.
TARGETS = {
    "windows": {"platforms": ("win_amd64",), "extra": ["colorama"], "own": ".bat", "other": (".command", "-MAC.md")},
    "mac": {"platforms": ("macosx_11_0_arm64", "macosx_10_13_x86_64"), "extra": [], "own": ".command",
            "other": (".bat",)},
}


def pip(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "pip", *args, "--disable-pip-version-check", "-q"], check=True)


def download_wheels(name: str) -> Path:
    t = TARGETS[name]
    wheels = BUILD / f"wheels-{name}"
    wheels.mkdir(parents=True, exist_ok=True)
    req = ["-r", str(ROOT / "requirements.txt"), *t["extra"]]
    for plat in t["platforms"]:
        for version in PY_VERSIONS:
            common = ["--only-binary=:all:", "--platform", plat, "--python-version", version, "--implementation", "cp"]
            pip("download", *common, "-d", str(wheels), *req)
            # Проверяем, что из папки ставится всё — для каждой версии Python и процессора.
            pip("install", "--dry-run", "--no-index", "--find-links", str(wheels), *common,
                "--target", str(BUILD / "check"), *req)
    return wheels


def build(name: str, files: list[str]) -> Path:
    t = TARGETS[name]
    wheels = download_wheels(name)
    target = ROOT / "dist" / f"Platforma-{config.APP_VERSION}-{name}.zip"
    target.parent.mkdir(exist_ok=True)
    forbidden = ("secret.key", "platform.db", ".env", "control.token")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            if any(f.endswith(x) for x in forbidden):
                raise SystemExit(f"В архив чуть не попал секретный файл: {f}")
            if "/" not in f and f.endswith(t["other"]):
                continue
            info = zipfile.ZipInfo.from_file(ROOT / f, f"Platforma/{f}")
            info.compress_type = zipfile.ZIP_DEFLATED
            # На Mac файл запуска открывается двойным щелчком, только если он исполняемый.
            info.external_attr = ((0o755 if f.endswith(".command") else 0o644) | 0o100000) << 16
            zf.writestr(info, (ROOT / f).read_bytes())
        for w in sorted(wheels.glob("*.whl")):
            zf.write(w, f"Platforma/wheels/{w.name}")
    names = zipfile.ZipFile(target).namelist()
    bad = [n for n in names if any(x in n for x in ("/data/", "/logs/", "/venv/", "/backups/", "secret.key", ".db"))]
    if bad:
        raise SystemExit(f"В архиве лишнее: {bad}")
    if not any(n.endswith(t["own"]) for n in names):
        raise SystemExit(f"В архиве {name} нет файлов запуска {t['own']}")
    print(f"{target} — файлов: {len(names)}, библиотек: {len(list(wheels.glob('*.whl')))}, "
          f"{target.stat().st_size / 1024 / 1024:.1f} МБ")
    return target


def main() -> int:
    files = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout.decode().split("\0")
    files = [f for f in files if f and not f.startswith(SKIP_PREFIXES)]
    for name in (sys.argv[1:] or list(TARGETS)):
        build(name, files)
    return 0


if __name__ == "__main__":
    sys.exit(main())
