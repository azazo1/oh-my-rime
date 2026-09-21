"""配置文件版本迁移.

配置里必须带 config_version, 迁移逐级进行, 不做 "看起来能跑就行" 的兼容处理.
新增版本时:
1. 提高 CURRENT_VERSION;
2. 写一个 _v{n}_to_v{n+1} 函数并登记到 _MIGRATIONS.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

CURRENT_VERSION = 2

log = logging.getLogger(__name__)

Migration = Callable[[dict[str, Any]], dict[str, Any]]


def _v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """v1 用 queue_dir/cache_dir/log_dir 三个绝对路径, v2 改成单一的 runtime_dir.

    三个目录本来就应该同根 (Lua 侧也只是把它拆成三段), 所以这里取它们的公共父目录;
    如果它们不在同一处, 以 queue_dir 为准并在日志里点名另外两个, 不做静默丢弃.
    """
    if "runtime_dir" in data:
        for key in ("queue_dir", "cache_dir", "log_dir"):
            data.pop(key, None)
        return data

    explicit = {key: data.pop(key, None) for key in ("queue_dir", "cache_dir", "log_dir")}
    parents = {
        Path(value).expanduser().parent for value in explicit.values() if value
    }
    if not parents:
        return data
    if len(parents) == 1:
        data["runtime_dir"] = str(parents.pop())
        return data

    chosen = Path(explicit["queue_dir"]).expanduser().parent
    others = {
        key: value
        for key, value in explicit.items()
        if value and Path(value).expanduser().parent != chosen
    }
    log.warning(
        "配置里三个目录不同根, 已按 queue_dir 收敛为 runtime_dir=%s; 被忽略的项: %s",
        chosen,
        ", ".join(f"{key}={value}" for key, value in others.items()),
    )
    data["runtime_dir"] = str(chosen)
    return data


_MIGRATIONS: dict[int, Migration] = {1: _v1_to_v2}


class ConfigMigrationError(Exception):
    """配置版本无法迁移."""


def migrate(raw: dict[str, Any], current: int = CURRENT_VERSION) -> Any:
    """把原始配置字典迁移到当前版本, 返回 Config 对象."""
    from dataclasses import fields

    from .config import Config, ConfigError

    version = raw.get("config_version")
    if version is None:
        raise ConfigMigrationError(
            "配置缺少 config_version, 请重新生成默认配置 (jev-bridge init-config)"
        )
    if not isinstance(version, int) or version < 1:
        raise ConfigMigrationError(f"config_version 非法: {version!r}")
    if version > current:
        raise ConfigMigrationError(
            f"配置版本 {version} 高于本程序支持的 {current}, 请升级 jev-bridge"
        )

    data = dict(raw)
    while version < current:
        migration = _MIGRATIONS.get(version)
        if migration is None:
            raise ConfigMigrationError(f"缺少 v{version} -> v{version + 1} 的迁移实现")
        data = migration(data)
        version += 1
        data["config_version"] = version

    data.pop("config_version", None)
    known = {item.name for item in fields(Config)}
    unknown = set(data) - known
    if unknown:
        # 未知项在迁移之后才检查: 旧版本的字段已经被迁移吃掉, 剩下的才是真的写错了
        raise ConfigError(
            f"配置存在未知项: {', '.join(sorted(unknown))}; 可用项: {', '.join(sorted(known))}"
        )
    try:
        return Config(**data)
    except TypeError as exc:
        raise ConfigMigrationError(f"配置项与当前版本不匹配: {exc}") from exc
