"""custom 文件的 marker 块注入/移除."""

from __future__ import annotations

from pathlib import Path

import pytest

from jev_bridge.rime_patch import (
    BEGIN_MARK,
    END_MARK,
    PatchError,
    apply_patch,
    default_snippet_path,
    remove_patch,
)

SAMPLE = """# 用户自己的注释
patch:
  "switches/@0/reset": 1
  menu/page_size: 4
  模糊音引用: 模糊音
模糊音:
  __append:
    - derive/^l/n
"""


@pytest.fixture
def custom_file(tmp_path: Path) -> Path:
    path = tmp_path / "wanxiang.custom.yaml"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


def test_apply_inserts_block(custom_file: Path, tmp_path: Path) -> None:
    result = apply_patch(custom_file, backup_dir=tmp_path / "backup")
    assert result.changed is True
    text = custom_file.read_text(encoding="utf-8")
    assert text.startswith("# 用户自己的注释\npatch:")
    assert BEGIN_MARK in text and END_MARK in text
    assert '"engine/filters/+"' in text
    assert text.count(BEGIN_MARK) == 1
    assert result.backup is not None and result.backup.exists()


def test_block_goes_inside_patch_before_named_fragments(custom_file: Path, tmp_path: Path) -> None:
    """补丁必须插在 patch: 之内, 且在顶层命名片段 (模糊音 等) 之前."""
    apply_patch(custom_file, backup_dir=tmp_path / "backup")
    lines = custom_file.read_text(encoding="utf-8").splitlines()
    begin = lines.index(BEGIN_MARK)
    end = lines.index(END_MARK)
    fragment = next(i for i, line in enumerate(lines) if line.startswith("模糊音:"))
    patch_line = next(i for i, line in enumerate(lines) if line.strip() == "patch:")
    assert patch_line < begin < end < fragment


def test_apply_is_idempotent(custom_file: Path, tmp_path: Path) -> None:
    apply_patch(custom_file, backup_dir=tmp_path / "backup")
    first = custom_file.read_text(encoding="utf-8")
    second = apply_patch(custom_file, backup_dir=tmp_path / "backup")
    assert second.changed is False
    assert custom_file.read_text(encoding="utf-8") == first


def test_remove_restores_original(custom_file: Path, tmp_path: Path) -> None:
    apply_patch(custom_file, backup_dir=tmp_path / "backup")
    result = remove_patch(custom_file, backup_dir=tmp_path / "backup")
    assert result.changed is True
    assert custom_file.read_text(encoding="utf-8") == SAMPLE
    assert remove_patch(custom_file).changed is False


def test_dry_run_does_not_touch_file(custom_file: Path, tmp_path: Path) -> None:
    before = custom_file.read_text(encoding="utf-8")
    result = apply_patch(custom_file, backup_dir=tmp_path / "backup", dry_run=True)
    assert result.changed is True
    assert "+" in result.diff
    assert custom_file.read_text(encoding="utf-8") == before


def test_refuses_when_no_patch_key(tmp_path: Path) -> None:
    path = tmp_path / "wanxiang.custom.yaml"
    path.write_text("other:\n  b: 2\n", encoding="utf-8")
    with pytest.raises(PatchError):
        apply_patch(path, backup_dir=tmp_path / "backup")


def test_fails_on_unpaired_marker(tmp_path: Path) -> None:
    path = tmp_path / "wanxiang.custom.yaml"
    path.write_text(f"patch:\n  a: 1\n{BEGIN_MARK}\n", encoding="utf-8")
    with pytest.raises(PatchError):
        apply_patch(path, backup_dir=tmp_path / "backup")


def test_snippet_is_indented_for_patch_mapping() -> None:
    lines = default_snippet_path().read_text(encoding="utf-8").splitlines()
    assert lines
    for line in lines:
        if line.strip():
            assert line.startswith("  "), line
