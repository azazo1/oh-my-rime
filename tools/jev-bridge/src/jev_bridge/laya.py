"""在完全隔离的环境里拉起本机 Laya 后端 (localjev-mlx).

不跑它的 install.sh: 那一步会建 ~/.localjev-mlx, 写 ~/.config/localjev-mlx, 装 launchd 常驻服务,
还会往 ~/.claude / ~/.codex / ~/.cursor / ~/.grok 里塞 skill —— 这些我们都不需要.

这里的做法:
- 用 `uv tool run --from <git+url>` 在 uv 自己的临时环境里跑 localjev-mlx, 不碰我们的项目 venv;
- 模型权重跟随 huggingface-hub 的默认落点 (~/.cache/huggingface, 用户设了 HF_HOME 就听 HF_HOME);
- 进程号与日志写在 <runtime_dir>/{laya.pid,log/laya.log}, 需要常驻就 --background, 想停就 --stop.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

LAYA_PACKAGE = "localjev-mlx[laya-mlx] @ git+https://github.com/rimusz/localjev-mlx.git"
DEFAULT_PYTHON = "3.12"  # laya-mlx 作者实测的版本
DEFAULT_PORT = 8090
DEFAULT_REPO = "convaiinnovations/laya"
PID_NAME = "laya.pid"
LOG_NAME = "laya.log"


def tool_command(port: int, python: str = DEFAULT_PYTHON) -> list[str]:
    """启动命令; uv 会把这个包装进它自己的临时环境, 与项目 venv 无关."""
    uv = shutil.which("uv") or "uv"
    return [
        uv,
        "tool",
        "run",
        "--python",
        python,
        "--from",
        LAYA_PACKAGE,
        "localjev-mlx",
        "serve",
        "--bind",
        "127.0.0.1",
        "--judge-port",
        str(port),
    ]


def hf_cache_dir() -> Path:
    """权重落点: 跟随 huggingface-hub 的默认值, 用户设了 HF_HOME 就以它为准."""
    override = os.environ.get("HF_HOME")
    if override:
        return Path(override).expanduser()
    return Path("~/.cache/huggingface").expanduser()


def sanitize_no_proxy(value: str) -> str:
    """去掉 NO_PROXY 里方括号形式的 IPv6 条目.

    huggingface_hub 用 httpx 建客户端时会把 NO_PROXY 当 URL 解析, 而 httpx 会把 `[::1]`
    读成 "主机 + 端口 :1]" 并抛 InvalidURL, 于是权重下载直接失败 (实测就是这个原因).
    去掉后少绕一个回环地址的代理, 对本地无影响; 代理本身 (HTTP(S)_PROXY) 保持不动.
    """
    parts = [item.strip() for item in (value or "").split(",")]
    return ",".join(item for item in parts if item and "[" not in item and "]" not in item)


def process_env(
    runtime_dir: Path, port: int, repo: str = DEFAULT_REPO, subfolder: str = ""
) -> dict:
    """只设启动必需的变量; 不覆盖 HF_HOME, 权重按 huggingface-hub 的默认位置存."""
    env = dict(os.environ)
    env.update(
        {
            "USE_TF": "0",
            "LOCALJEV_BACKEND": "laya-mlx",
            "LOCALJEV_BIND": "127.0.0.1",
            "LOCALJEV_JUDGE_PORT": str(port),
            "LOCALJEV_REPO": repo,
        }
    )
    if subfolder:
        env["LOCALJEV_SUBFOLDER"] = subfolder
    for name in ("NO_PROXY", "no_proxy"):
        if name in env:
            env[name] = sanitize_no_proxy(env[name])
    return env


def pid_file(runtime_dir: Path) -> Path:
    return runtime_dir / PID_NAME


def log_file(runtime_dir: Path) -> Path:
    return runtime_dir / "log" / LOG_NAME


@dataclass
class LayaStatus:
    running: bool
    pid: int | None
    health: dict | None
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "pid": self.pid,
            "health": self.health,
            "error": self.error,
        }


def read_pid(runtime_dir: Path) -> int | None:
    path = pid_file(runtime_dir)
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def is_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def health(port: int, timeout_s: float = 2.0) -> tuple[int, dict | None]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout_s) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
            return int(resp.status), body
    except urllib.error.HTTPError as exc:
        return int(exc.code), None
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return 0, None


def status(runtime_dir: Path, port: int) -> LayaStatus:
    pid = read_pid(runtime_dir)
    code, body = health(port)
    if code == 200:
        return LayaStatus(running=True, pid=pid, health=body)
    if is_alive(pid):
        return LayaStatus(running=True, pid=pid, health=None, error="模型还在加载 (health 未 200)")
    return LayaStatus(running=False, pid=pid, health=None, error="没有在跑")


def stop(runtime_dir: Path, timeout_s: float = 10.0) -> bool:
    pid = read_pid(runtime_dir)
    if not is_alive(pid):
        pid_file(runtime_dir).unlink(missing_ok=True)
        return False
    assert pid is not None
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pid_file(runtime_dir).unlink(missing_ok=True)
        return False
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not is_alive(pid):
            pid_file(runtime_dir).unlink(missing_ok=True)
            return True
        time.sleep(0.2)
    os.kill(pid, signal.SIGKILL)
    pid_file(runtime_dir).unlink(missing_ok=True)
    return True


def start_background(
    runtime_dir: Path,
    port: int,
    python: str = DEFAULT_PYTHON,
    repo: str = DEFAULT_REPO,
    subfolder: str = "",
) -> int:
    """后台起服务, 返回进程号; 日志写到 <runtime_dir>/log/laya.log."""
    runtime_dir.mkdir(parents=True, exist_ok=True)
    log_file(runtime_dir).parent.mkdir(parents=True, exist_ok=True)
    handle = log_file(runtime_dir).open("a", encoding="utf-8")
    handle.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 localjev-mlx (port {port}) ===\n")
    handle.flush()
    process = subprocess.Popen(
        tool_command(port, python),
        env=process_env(runtime_dir, port, repo, subfolder),
        stdout=handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    pid_file(runtime_dir).write_text(str(process.pid), encoding="utf-8")
    return process.pid


def wait_until_ready(port: int, wait_s: float, on_progress=None) -> tuple[bool, dict | None]:
    """轮询 /health: 首次要下载权重, 所以默认给很长的等待时间."""
    deadline = time.time() + wait_s
    started = time.time()
    while time.time() < deadline:
        code, body = health(port)
        if code == 200:
            return True, body
        if on_progress:
            on_progress(int(time.time() - started), code)
        time.sleep(3)
    return False, None


def run_foreground(
    runtime_dir: Path,
    port: int,
    python: str = DEFAULT_PYTHON,
    repo: str = DEFAULT_REPO,
    subfolder: str = "",
) -> int:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    python_bin = tool_command(port, python)
    print(
        f"前台启动 localjev-mlx: 端口 {port}, 权重缓存 {hf_cache_dir()}\n"
        f"  {' '.join(python_bin)}\n"
        "  首次启动要下载权重, /health 变 200 之前输入法侧会走原序放行。Ctrl-C 退出。",
        file=sys.stderr,
        flush=True,
    )
    return subprocess.run(
        python_bin, env=process_env(runtime_dir, port, repo, subfolder), check=False
    ).returncode
