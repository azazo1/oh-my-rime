"""跨语言一致的缓存键与归一化规则.

本模块与 lua/jev/jev_client.lua 必须给出完全相同的键, 否则 Lua 无法直接命中
sidecar 写下的缓存文件, 每次按键都会退化成同步等待. 两侧共享测试向量见
tests/fixtures/key_vectors.json.

键串格式: v1|<schema>|<code>|<context>|<prompt_version>|<候选文本以 \\x1f 连接>
"""

from __future__ import annotations

PROTOCOL_VERSION = 1
PROMPT_VERSION = 3
KEY_PREFIX = "v1"
CANDIDATE_SEPARATOR = "\x1f"

_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x100000001B3
_FNV_MASK = (1 << 64) - 1

_CODE_STRIP = str.maketrans("", "", " \t\r\n\f\v")
_TEXT_MAP = str.maketrans({"\r": " ", "\n": " ", "\t": " "})


def fnv1a64_hex(text: str) -> str:
    """FNV-1a 64 位哈希, 输入按 UTF-8 字节处理, 输出 16 位小写十六进制."""
    digest = _FNV_OFFSET
    for byte in text.encode("utf-8"):
        digest ^= byte
        digest = (digest * _FNV_PRIME) & _FNV_MASK
    return f"{digest:016x}"


def normalize_text(text: str) -> str:
    """把换行与制表符折叠成空格, 其余原样保留."""
    return (text or "").translate(_TEXT_MAP)


def normalize_code(code: str) -> str:
    """去掉所有空白并转小写."""
    return (code or "").translate(_CODE_STRIP).lower()


def tail_chars(text: str, limit: int) -> str:
    """取末尾 limit 个码点 (按字符而不是字节截断)."""
    if limit <= 0:
        return ""
    return text[-limit:]


def normalize_context(context: str, limit: int | None = None) -> str:
    """归一化上文. limit 为 None 表示调用方已经截断过."""
    text = normalize_text(context)
    if limit is not None:
        text = tail_chars(text, limit)
    return text.strip(" ")


def join_candidates(candidates: list[str]) -> str:
    return CANDIDATE_SEPARATOR.join(normalize_text(text) for text in candidates)


def key_source(
    schema_id: str,
    code: str,
    context: str,
    candidate_texts: list[str],
    prompt_version: int = PROMPT_VERSION,
) -> str:
    """拼出参与哈希的原始字符串, 便于排障时对比两侧. context 视为已截断."""
    return "|".join(
        [
            KEY_PREFIX,
            schema_id or "",
            normalize_code(code),
            normalize_context(context),
            str(prompt_version),
            join_candidates(candidate_texts),
        ]
    )


def cache_key(
    schema_id: str,
    code: str,
    context: str,
    candidate_texts: list[str],
    prompt_version: int = PROMPT_VERSION,
) -> str:
    return fnv1a64_hex(
        key_source(schema_id, code, context, candidate_texts, prompt_version)
    )
