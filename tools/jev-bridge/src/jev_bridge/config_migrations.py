"""配置文件版本迁移.

配置里必须带 config_version, 迁移逐级进行, 不做 "看起来能跑就行" 的兼容处理.
新增版本时:
1. 提高 CURRENT_VERSION;
2. 写一个 _v{n}_to_v{n+1} 函数并登记到 _MIGRATIONS.
"""

from __future__ import annotations

from typing import Any, Callable

CURRENT_VERSION = 1

Migration = Callable[[dict[str, Any]], dict[str, Any]]

_MIGRATIONS: dict[int, Migration] = {}


class ConfigMigrationError(Exception):
    """配置版本无法迁移."""


def migrate(raw: dict[str, Any], current: int = CURRENT_VERSION) -> Any:
    """把原始配置字典迁移到当前版本, 返回 Config 对象."""
    from .config import Config

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
    try:
        return Config(**data)
    except TypeError as exc:
        raise ConfigMigrationError(f"配置项与当前版本不匹配: {exc}") from exc
