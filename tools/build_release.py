"""Собирает архив для установки на сервер: программа + библиотеки для Windows (без интернета).

    python tools/build_release.py

Результат: dist/Platforma-<версия>.zip. В архив не попадают data, logs, backups, venv, тесты
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

WHEELS = ROOT / "build" / "wheels"
SKIP_PREFIXES = ("tests/", "tools/", "docs/screenshots/", ".git")
PY_VERSIONS = ("3.11", "3.12", "3.13")


def download_wheels() -> None:
    WHEELS.mkdir(parents=True, exist_ok=True)
    for version in PY_VERSIONS:
        subprocess.run([sys.executable, "-m", "pip", "download", "-q", "--only-binary=:all:", "--platform", "win_amd64",
                        "--python-version", version, "--implementation", "cp", "-d", str(WHEELS),
                        "-r", str(ROOT / "requirements.txt"), "colorama"], check=True)
    # Проверяем, что из папки ставится всё — для каждой версии Python.
    for version in PY_VERSIONS:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--dry-run", "--no-index", "--find-links",
                        str(WHEELS), "--only-binary=:all:", "--platform", "win_amd64", "--python-version", version,
                        "--implementation", "cp", "--target", str(ROOT / "build" / "check"),
                        "-r", str(ROOT / "requirements.txt"), "colorama"], check=True)


def main() -> int:
    download_wheels()
    files = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout.decode().split("\0")
    files = [f for f in files if f and not f.startswith(SKIP_PREFIXES)]
    target = ROOT / "dist" / f"Platforma-{config.APP_VERSION}.zip"
    target.parent.mkdir(exist_ok=True)
    forbidden = ("secret.key", "platform.db", ".env", "control.token")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            if any(f.endswith(x) for x in forbidden):
                raise SystemExit(f"В архив чуть не попал секретный файл: {f}")
            zf.write(ROOT / f, f"Platforma/{f}")
        for w in sorted(WHEELS.glob("*.whl")):
            zf.write(w, f"Platforma/wheels/{w.name}")
    names = zipfile.ZipFile(target).namelist()
    bad = [n for n in names if any(x in n for x in ("/data/", "/logs/", "/venv/", "/backups/", "secret.key", ".db"))]
    if bad:
        raise SystemExit(f"В архиве лишнее: {bad}")
    print(f"{target} — файлов: {len(names)}, библиотек: {len(list(WHEELS.glob('*.whl')))}, "
          f"{target.stat().st_size / 1024 / 1024:.1f} МБ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
