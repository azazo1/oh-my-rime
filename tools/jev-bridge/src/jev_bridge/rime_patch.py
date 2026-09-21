"""把 rime-patch/jev-rerank.patch.yaml 以 marker 块的形式并入 Rime 的 custom 文件.

为什么不用 YAML 库整文件重写: 用户的 wanxiang.custom.yaml 全是带注释的手写配置,
任何 dump 都会把注释和排版毁掉. 因此这里只做 "在文件末尾插入一段带标记的文本",
并且严格检查插入位置确实还在顶层的 patch: 映射之内.
"""

from __future__ import annotations

import difflib
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from . import paths

BEGIN_MARK = "# >>> jev-rerank begin (由 tools/jev-bridge 管理, 不要手改本块)"
END_MARK = "# <<< jev-rerank end"
# 片段里这个占位符在写入时替换成本机实际路径, 所以 Rime 侧不需要自己猜平台
RUNTIME_DIR_PLACEHOLDER = "__JEV_RUNTIME_DIR__"


class PatchError(Exception):
    """目标文件结构不符合预期, 拒绝自动改写."""


@dataclass
class PatchResult:
    path: Path
    changed: bool
    backup: Path | None
    diff: str

    def summary(self) -> str:
        if not self.changed:
            return f"{self.path} 已是最新, 无需修改"
        backup = f", 备份 {self.backup}" if self.backup else ""
        return f"{self.path} 已更新{backup}"


def default_snippet_path() -> Path:
    return Path(__file__).resolve().parents[2] / "rime-patch" / "jev-rerank.patch.yaml"


def default_backup_dir() -> Path:
    return paths.default_runtime_dir() / "backup"


def render_snippet(snippet: str, runtime_dir: Path | None = None) -> str:
    """把片段里的占位符换成本机实际路径 (HOME 之下写成 ~ 形式, 便于跨机器复用)."""
    target = runtime_dir or paths.default_runtime_dir()
    return snippet.replace(RUNTIME_DIR_PLACEHOLDER, paths.display_path(target))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_block(text: str) -> tuple[str, bool]:
    """摘掉已有的 marker 块 (含前后多余空行), 返回 (新文本, 是否摘掉过)."""
    lines = text.splitlines()
    begin = next((i for i, line in enumerate(lines) if line.strip() == BEGIN_MARK), None)
    if begin is None:
        return text, False
    end = next((i for i, line in enumerate(lines) if line.strip() == END_MARK), None)
    if end is None or end < begin:
        raise PatchError("发现 begin 标记但没有配对的 end 标记, 请手工检查")
    start = begin
    while start > 0 and lines[start - 1].strip() == "":
        start -= 1
    stop = end + 1
    while stop < len(lines) and lines[stop].strip() == "":
        stop += 1
    remaining = lines[:start] + lines[stop:]
    return "\n".join(remaining).rstrip("\n") + "\n", True


def _is_top_level_key(line: str) -> bool:
    if line.startswith((" ", "\t")):
        return False
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _patch_block_end(lines: list[str]) -> int:
    """返回 patch: 映射块的结束位置 (第一个后续顶格键的行号, 没有则是文件末尾).

    万象的 custom 文件在 patch: 之后还会有顶层的命名片段 (模糊音 / 18jian / 14jian),
    它们是给 patch 内部引用的, 所以补丁必须插在它们之前.
    """
    start = None
    for index, line in enumerate(lines):
        if _is_top_level_key(line) and line.strip().split(":", 1)[0].strip() == "patch":
            start = index
            break
    if start is None:
        raise PatchError("custom 文件里找不到顶层的 patch: 段, 拒绝自动改写")
    for index in range(start + 1, len(lines)):
        if _is_top_level_key(lines[index]):
            return index
    return len(lines)


def _compose(original: str, snippet: str) -> str:
    body = snippet.strip("\n")
    if not body:
        raise PatchError("补丁片段为空")
    for line in body.splitlines():
        if line.strip() and not line.startswith("  "):
            raise PatchError(f"补丁片段必须整体缩进两格, 越界行: {line!r}")

    lines = original.rstrip("\n").splitlines()
    index = _patch_block_end(lines)
    while index > 0 and lines[index - 1].strip() == "":
        index -= 1
    block = [BEGIN_MARK, *body.splitlines(), END_MARK]
    merged = lines[:index] + ["", *block, ""] + lines[index:]
    return "\n".join(merged).rstrip("\n") + "\n"


def _backup(path: Path, backup_dir: Path | None) -> Path:
    directory = backup_dir or default_backup_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = directory / f"{path.name}.{stamp}.bak"
    shutil.copy2(path, target)
    return target


def _diff(old: str, new: str, path: Path) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"{path} (修改前)",
            tofile=f"{path} (修改后)",
        )
    )


def apply_patch(
    target: Path,
    snippet_path: Path | None = None,
    dry_run: bool = False,
    backup_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> PatchResult:
    if not target.exists():
        raise PatchError(f"目标文件不存在: {target}")
    snippet = render_snippet(
        _read(snippet_path or default_snippet_path()), runtime_dir=runtime_dir
    )
    original = _read(target)
    stripped, _ = _strip_block(original)
    updated = _compose(stripped, snippet)
    if updated == original:
        return PatchResult(target, changed=False, backup=None, diff="")
    diff = _diff(original, updated, target)
    if dry_run:
        return PatchResult(target, changed=True, backup=None, diff=diff)
    backup = _backup(target, backup_dir)
    target.write_text(updated, encoding="utf-8")
    return PatchResult(target, changed=True, backup=backup, diff=diff)


def remove_patch(
    target: Path, dry_run: bool = False, backup_dir: Path | None = None
) -> PatchResult:
    if not target.exists():
        raise PatchError(f"目标文件不存在: {target}")
    original = _read(target)
    stripped, found = _strip_block(original)
    if not found:
        return PatchResult(target, changed=False, backup=None, diff="")
    diff = _diff(original, stripped, target)
    if dry_run:
        return PatchResult(target, changed=True, backup=None, diff=diff)
    backup = _backup(target, backup_dir)
    target.write_text(stripped, encoding="utf-8")
    return PatchResult(target, changed=True, backup=backup, diff=diff)
