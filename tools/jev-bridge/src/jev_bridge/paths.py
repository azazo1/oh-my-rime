"""按平台推导默认路径.

这里是"路径自动识别"的唯一实现, Rime 侧的 lua/jev/jev_client.lua 用同一套规则
(macOS: ~/Library/Caches, 其它 Unix: $XDG_CACHE_HOME 或 ~/.cache, Windows: %LOCALAPPDATA%),
两侧不一致时 `jev-bridge paths --check` 会直接报出来.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

RUNTIME_DIR_NAME = "rime-jev"


def platform_name() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def home_dir() -> Path:
    return Path.home()


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    return Path(raw).expanduser()


def default_runtime_dir() -> Path:
    """queue/cache/log 三个目录共同的父目录 (仓库之外, 不进版本控制)."""
    override = _env_path("JEV_RUNTIME_DIR")
    if override is not None:
        return override
    platform = platform_name()
    if platform == "macos":
        return home_dir() / "Library" / "Caches" / RUNTIME_DIR_NAME
    if platform == "windows":
        base = _env_path("LOCALAPPDATA") or (home_dir() / "AppData" / "Local")
        return base / RUNTIME_DIR_NAME
    base = _env_path("XDG_CACHE_HOME") or (home_dir() / ".cache")
    return base / RUNTIME_DIR_NAME


def rime_user_dir_candidates() -> list[Path]:
    """各前端默认的 Rime 用户目录, 按优先级排列."""
    platform = platform_name()
    if platform == "macos":
        return [home_dir() / "Library" / "Rime"]
    if platform == "windows":
        base = _env_path("APPDATA") or (home_dir() / "AppData" / "Roaming")
        return [base / "Rime"]
    data_home = _env_path("XDG_DATA_HOME") or (home_dir() / ".local" / "share")
    config_home = _env_path("XDG_CONFIG_HOME") or (home_dir() / ".config")
    return [
        data_home / "fcitx5" / "rime",  # fcitx5-rime
        config_home / "ibus" / "rime",  # ibus-rime
        data_home / "fcitx" / "rime",  # 旧版 fcitx
    ]


def resolve_rime_user_dir(explicit: str | Path | None = None) -> Path:
    """RIME_USER_DIR / --rime-dir 优先; 否则取第一个存在的候选, 都不存在时给第一个."""
    if explicit:
        return Path(explicit).expanduser()
    override = _env_path("RIME_USER_DIR")
    if override is not None:
        return override
    candidates = rime_user_dir_candidates()
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def schema_patch_target(explicit: str | Path | None = None) -> Path:
    return resolve_rime_user_dir(explicit) / "wanxiang.custom.yaml"


def display_path(path: Path) -> str:
    """HOME 之下的路径写成 ~ 形式, 便于跨机器复用 (Lua 侧会展开 ~)."""
    try:
        relative = path.relative_to(home_dir())
    except ValueError:
        return path.as_posix()
    return "~/" + relative.as_posix()


def as_dict(explicit_rime_dir: str | Path | None = None) -> dict:
    runtime = default_runtime_dir()
    rime_dir = resolve_rime_user_dir(explicit_rime_dir)
    return {
        "platform": platform_name(),
        "home": str(home_dir()),
        "runtime_dir": str(runtime),
        "runtime_dir_display": display_path(runtime),
        "queue_dir": str(runtime / "queue"),
        "cache_dir": str(runtime / "cache"),
        "log_dir": str(runtime / "log"),
        "rime_user_dir": str(rime_dir),
        "rime_user_dir_exists": rime_dir.exists(),
        "rime_user_dir_candidates": [str(path) for path in rime_user_dir_candidates()],
        "schema_patch_target": str(rime_dir / "wanxiang.custom.yaml"),
    }
