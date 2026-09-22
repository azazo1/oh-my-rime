"""命令行入口: serve / once / simulate / rerank / bench / health / paths / patch-config."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__, laya, paths
from .backends import build_backend
from .cache import DiskCache
from .config import Config, dump_default_config, load_config
from .keys import PROMPT_VERSION, PROTOCOL_VERSION, cache_key, normalize_text
from .logs import setup_logging
from .rerank import Reranker
from .rime_patch import (
    BEGIN_MARK,
    END_MARK,
    PatchError,
    apply_patch,
    default_snippet_path,
    remove_patch,
)
from .server import backend_identity, build_server

RUNTIME_DIR_PATTERN = re.compile(r'^\s*runtime_dir:\s*"?([^"\n]+)"?\s*$', re.MULTILINE)

_id_counter = 0


def _new_id() -> str:
    """与 Lua 侧同构的请求 id: 时间 + 计数 + 随机, 保证按时间大致有序."""
    global _id_counter
    _id_counter += 1
    return f"{int(time.time()):x}{_id_counter % 0x1000000:06x}{random.randint(0, 0xFFFF):04x}"


def _build_request(args: argparse.Namespace, config: Config) -> dict:
    texts = [text for text in (args.candidates or "").split(",") if text]
    if not texts:
        texts = ["你好", "尼豪", "你", "拟好"]
    candidates = [{"index": i, "text": text} for i, text in enumerate(texts)]
    context = args.context or ""
    mock_probabilities = None
    if getattr(args, "mock_probabilities", None):
        mock_probabilities = {}
        for pair in args.mock_probabilities.split(","):
            key, _, value = pair.partition(":")
            mock_probabilities[key.strip()] = float(value)
    key = cache_key(args.schema_id, args.code, context, texts, PROMPT_VERSION)
    return {
        "v": PROTOCOL_VERSION,
        "id": _new_id(),
        "ts": time.time(),
        "mode": args.mode,
        "schema_id": args.schema_id,
        "code": args.code,
        "context": context,
        "candidates": candidates,
        "question": {
            "name": "best_continuation",
            "type": "choice",
            "instructions": (
                "Given the text the user has already typed (context) and the code being "
                "typed, which candidate is most likely intended next? "
                "Prefer the candidate that fits the context naturally."
            ),
            "criteria": {str(i): text for i, text in enumerate(texts)},
        },
        "prompt_version": PROMPT_VERSION,
        "cache_key": key,
        "badge": config.badge,
        "show_confidence": config.show_confidence,
        "mock_probabilities": mock_probabilities,
    }


def _load_or_exit(args: argparse.Namespace) -> Config:
    try:
        return load_config(Path(args.config) if args.config else None)
    except Exception as exc:  # noqa: BLE001 - CLI 顶层需要友好报错
        print(f"配置读取失败: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


# --------------------------------------------------------------------- 子命令


def cmd_serve(args: argparse.Namespace) -> int:
    config = _load_or_exit(args)
    config.ensure_dirs()
    setup_logging(config.log_path, config.log_level, config.debug)
    import logging

    log = logging.getLogger("jev_bridge")
    log.info("jev-bridge %s 启动", __version__)
    log.info(
        "配置: backend=%s base_url=%s model=%s allow_cloud=%s 队列=%s",
        config.backend,
        config.base_url,
        config.model,
        config.allow_cloud,
        config.queue_path,
    )
    cache = DiskCache(config.cache_path, config.cache_ttl_s, config.cache_max_entries)
    bridge = build_server(config, cache)
    bridge.start()
    stop_event = threading.Event()

    def _stop(signum, _frame):
        log.info("收到信号 %s, 准备退出", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        bridge.worker.run_forever(stop_event)
    finally:
        bridge.stop()
        log.info("缓存统计: %s", json.dumps(cache.stats.as_dict(), ensure_ascii=False))
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    config = _load_or_exit(args)
    config.ensure_dirs()
    setup_logging(config.log_path, config.log_level, config.debug)
    cache = DiskCache(config.cache_path, config.cache_ttl_s, config.cache_max_entries)
    bridge = build_server(config, cache)
    processed = bridge.worker.tick()
    print(
        json.dumps(
            {
                "processed_groups": processed,
                "queue": bridge.worker.stats.as_dict(),
                "cache": cache.stats.as_dict(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    """按 Lua 侧同样的格式投递一个请求并等待响应, 用于不打字就验证整条链路."""
    config = _load_or_exit(args)
    config.ensure_dirs()
    setup_logging(config.log_path, config.log_level, config.debug)
    request = _build_request(args, config)
    if not request.get("mock_probabilities"):
        request.pop("mock_probabilities", None)
    if getattr(args, "mock_error", None):
        request["mock_error"] = args.mock_error
    queue_dir = config.queue_path
    req_path = queue_dir / f"{request['id']}.req.json"
    res_path = queue_dir / f"{request['id']}.res.json"
    tmp = req_path.with_name(f"{req_path.name}.tmp")
    tmp.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    tmp.replace(req_path)
    deadline = time.time() + args.timeout_ms / 1000
    while time.time() < deadline:
        if res_path.exists():
            payload = json.loads(res_path.read_text(encoding="utf-8"))
            res_path.unlink(missing_ok=True)
            req_path.unlink(missing_ok=True)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0 if payload.get("ok") else 1
        time.sleep(0.005)
    print(f"等待响应超时 ({args.timeout_ms}ms), 请求仍在队列: {req_path}", file=sys.stderr)
    return 2


def cmd_rerank(args: argparse.Namespace) -> int:
    config = _load_or_exit(args)
    config.ensure_dirs()
    setup_logging(config.log_path, config.log_level, config.debug)
    raw = sys.stdin.read() if args.request in (None, "-") else Path(args.request).read_text("utf-8")
    request = json.loads(raw)
    cache = DiskCache(config.cache_path, config.cache_ttl_s, config.cache_max_entries)
    reranker = Reranker(config, cache, build_backend(config))
    print(json.dumps(reranker.handle(request), ensure_ascii=False, indent=2))
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    config = _load_or_exit(args)
    config.ensure_dirs()
    setup_logging(config.log_path, config.log_level, config.debug)
    cache = DiskCache(config.cache_path, config.cache_ttl_s, config.cache_max_entries)
    backend = build_backend(config)
    reranker = Reranker(config, cache, backend)
    latencies: list[float] = []
    for index in range(args.count):
        args.context = f"第 {index} 次基准测试的上文"
        args.code = f"code{index}"
        request = _build_request(args, config)
        request.pop("mock_probabilities", None)
        started = time.perf_counter()
        response = reranker.handle(request)
        latencies.append((time.perf_counter() - started) * 1000)
        if not response.get("ok"):
            print(f"第 {index} 次失败: {response.get('error')}", file=sys.stderr)
    latencies.sort()
    if not latencies:
        return 1

    def _pct(p: float) -> float:
        return round(latencies[min(len(latencies) - 1, int(len(latencies) * p))], 2)

    print(
        json.dumps(
            {
                "backend": backend.name,
                "count": len(latencies),
                "p50_ms": _pct(0.50),
                "p95_ms": _pct(0.95),
                "max_ms": round(latencies[-1], 2),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    config = _load_or_exit(args)
    url = f"http://{config.http_host}:{config.http_port}/health"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            print(json.dumps(json.loads(response.read().decode("utf-8")), ensure_ascii=False, indent=2))
        return 0
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        print(f"健康检查失败 ({url}): {exc}", file=sys.stderr)
        return 1


def cmd_init_config(args: argparse.Namespace) -> int:
    path = dump_default_config(Path(args.config) if args.config else None)
    print(f"配置模板: {path}")
    return 0


def _schema_runtime_dir(rime_dir: Path) -> str | None:
    """从 wanxiang.custom.yaml 的 marker 块里读出实际写入的 runtime_dir."""
    target = paths.schema_patch_target(rime_dir)
    if not target.exists():
        return None
    text = target.read_text(encoding="utf-8")
    begin = text.find(BEGIN_MARK)
    end = text.find(END_MARK)
    if begin < 0 or end < begin:
        return None
    match = RUNTIME_DIR_PATTERN.search(text[begin:end])
    return match.group(1).strip() if match else None


def cmd_paths(args: argparse.Namespace) -> int:
    """打印按平台推导出的全部路径, 并核对 Rime 侧写的 runtime_dir 是否一致."""
    payload = paths.as_dict(args.rime_dir)
    schema_dir = _schema_runtime_dir(paths.resolve_rime_user_dir(args.rime_dir))
    payload["schema_runtime_dir"] = schema_dir

    mismatched = False
    if schema_dir:
        expected = Path(payload["runtime_dir"])
        actual = Path(schema_dir.replace("~", str(Path.home()), 1)).expanduser()
        matched = expected == actual
        payload["runtime_dir_match"] = matched
        mismatched = not matched
    else:
        payload["runtime_dir_match"] = None

    payload["ok"] = not mismatched
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if mismatched:
        print(
            "两侧 runtime_dir 不一致: sidecar 用 "
            f"{payload['runtime_dir']}, Rime 侧写的是 {schema_dir}; "
            "重新执行 patch-config 或手工对齐",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    config = _load_or_exit(args)
    log_file = config.log_path / "sidecar.log"
    if not log_file.exists():
        print(f"还没有日志: {log_file}", file=sys.stderr)
        return 1
    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-args.lines :]:
        print(line)
    return 0


def cmd_laya(args: argparse.Namespace) -> int:
    """隔离地管理本机 Laya 后端 (uv tool run, 不跑它的 install.sh)."""
    config = _load_or_exit(args)
    runtime = config.runtime_path
    port = args.port or laya.DEFAULT_PORT

    if args.stop:
        stopped = laya.stop(runtime)
        print("已停止 localjev-mlx" if stopped else "没有在跑的 localjev-mlx")
        return 0

    if args.status:
        payload = laya.status(runtime, port).as_dict()
        payload["port"] = port
        payload["log"] = str(laya.log_file(runtime))
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["running"] else 1

    if not args.background:
        return laya.run_foreground(runtime, port, args.python, args.repo, args.subfolder)

    if laya.status(runtime, port).running:
        print(f"已经在跑 (端口 {port}); 用 --stop 先停掉再起", file=sys.stderr)
        return 1
    pid = laya.start_background(runtime, port, args.python, args.repo, args.subfolder)
    print(f"已在后台启动 (pid {pid}), 日志 {laya.log_file(runtime)}", file=sys.stderr)
    print(f"等待 /health 变 200 (首次要下载权重, 最长等 {args.wait_s}s)...", file=sys.stderr)

    def _progress(elapsed: int, code: int) -> None:
        print(f"  {elapsed}s: health={code or 'down'}", file=sys.stderr)

    ready, body = laya.wait_until_ready(port, args.wait_s, _progress)
    if not ready:
        print("超时仍未就绪, 看日志确认是否在下载权重", file=sys.stderr)
        return 1
    print(
        f"就绪: backend={body.get('backend')} model={body.get('model')} port={port}",
        file=sys.stderr,
    )
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    """在 case 集上量首选率, 用于对比 checkpoint / 提示词."""
    from .evaluate import DEFAULT_INSTRUCTIONS, load_cases, render_report, run_case, summarize

    config = _load_or_exit(args)
    config.ensure_dirs()
    setup_logging(config.log_path, config.log_level, config.debug)
    cases = load_cases(Path(args.cases))
    cache = DiskCache(config.cache_path, config.cache_ttl_s, config.cache_max_entries)
    if args.fresh:
        print(f"已清空 {cache.clear()} 条缓存, 强制走真实后端", file=sys.stderr)
    backend = build_backend(config)
    cache.drop_if_backend_changed(backend_identity(config, backend))
    reranker = Reranker(config, cache, backend)
    instructions = args.instructions or DEFAULT_INSTRUCTIONS

    results = []
    for repeat in range(args.repeat):
        for index, case in enumerate(cases):
            results.append(run_case(reranker, case, index + repeat * len(cases), instructions))
    summary = summarize(results, tag=args.tag)
    summary["backend"] = backend.name
    summary["model"] = config.model
    print(render_report(results[: len(cases)], summary, verbose=not args.quiet))
    return 0 if summary.get("skipped", 0) == 0 else 1


def cmd_deploy_rime(args: argparse.Namespace) -> int:
    """各前端重新部署; 只做能确定的事情, 找不到工具时给出人话提示."""
    platform = paths.platform_name()
    if platform == "macos":
        binary = Path("/Library/Input Methods/Squirrel.app/Contents/MacOS/Squirrel")
        if not binary.exists():
            print(f"没找到鼠须管: {binary}", file=sys.stderr)
            return 1
        return subprocess.run([str(binary), "--reload"], check=False).returncode

    if platform == "windows":
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        deployers = sorted(program_files.glob("Rime/weasel-*/WeaselDeployer.exe"))
        if not deployers:
            print(
                "没找到 WeaselDeployer.exe, 请用托盘菜单里的「重新部署」", file=sys.stderr
            )
            return 1
        return subprocess.run([str(deployers[-1]), "/deploy"], check=False).returncode

    remote = shutil.which("fcitx5-remote")
    if remote:
        return subprocess.run([remote, "-r"], check=False).returncode
    print(
        "Linux 上请用输入法菜单里的「重新部署」(fcitx5: 输入法配置 -> 附加组件 -> Rime -> 部署)",
        file=sys.stderr,
    )
    return 1


def cmd_patch_config(args: argparse.Namespace) -> int:
    rime_dir = paths.resolve_rime_user_dir(args.rime_dir)
    target = paths.schema_patch_target(rime_dir)
    try:
        result = apply_patch(
            target,
            Path(args.snippet) if args.snippet else default_snippet_path(),
            dry_run=args.dry_run,
        )
    except PatchError as exc:
        print(f"补丁失败: {exc}", file=sys.stderr)
        return 2
    if result.diff and (args.dry_run or args.show_diff):
        print(result.diff)
    prefix = "(dry-run, 未落盘) " if args.dry_run else ""
    print(prefix + result.summary())
    if not args.dry_run:
        print(f"写入的 runtime_dir: {paths.display_path(paths.default_runtime_dir())}")
    return 0


def cmd_unpatch_config(args: argparse.Namespace) -> int:
    rime_dir = paths.resolve_rime_user_dir(args.rime_dir)
    target = paths.schema_patch_target(rime_dir)
    try:
        result = remove_patch(target, dry_run=args.dry_run)
    except PatchError as exc:
        print(f"移除失败: {exc}", file=sys.stderr)
        return 2
    if result.diff and (args.dry_run or args.show_diff):
        print(result.diff)
    prefix = "(dry-run, 未落盘) " if args.dry_run else ""
    print(prefix + result.summary())
    return 0


def cmd_version(_args: argparse.Namespace) -> int:
    print(__version__)
    return 0


def cmd_show_config(args: argparse.Namespace) -> int:
    """打印当前生效配置, 便于确认 env 覆盖是否按预期生效."""
    from .config import DEFAULT_CONFIG_PATH, as_dict

    config = _load_or_exit(args)
    payload = as_dict(config)
    payload["api_key"] = "***" if payload.get("api_key") else ""
    print(
        json.dumps(
            {
                "config_path": str(Path(args.config) if args.config else DEFAULT_CONFIG_PATH),
                "config_exists": (Path(args.config) if args.config else DEFAULT_CONFIG_PATH).exists(),
                "is_cloud": config.is_cloud,
                "effective": payload,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


# --------------------------------------------------------------------- LaunchAgent

LAUNCH_AGENT_LABEL = "ai.rime.jev-bridge"
LAUNCH_AGENT_PATH = Path("~/Library/LaunchAgents/ai.rime.jev-bridge.plist").expanduser()
PLIST_TEMPLATE = Path(__file__).resolve().parents[2] / "launchd" / "ai.rime.jev-bridge.plist"


def _render_plist(config: Config) -> str:
    binary = Path(sys.prefix) / "bin" / "jev-bridge"
    if not binary.exists():
        binary = Path(sys.executable).parent / "jev-bridge"
    template = PLIST_TEMPLATE.read_text(encoding="utf-8")
    return (
        template.replace("@VENV_BIN@", str(binary))
        .replace("@LOG_DIR@", str(config.log_path))
    )


def cmd_install_agent(args: argparse.Namespace) -> int:
    import subprocess

    config = _load_or_exit(args)
    config.ensure_dirs()
    LAUNCH_AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENT_PATH.write_text(_render_plist(config), encoding="utf-8")
    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", domain, str(LAUNCH_AGENT_PATH)], check=False
    )
    result = subprocess.run(
        ["launchctl", "bootstrap", domain, str(LAUNCH_AGENT_PATH)], check=False
    )
    if result.returncode != 0:
        result = subprocess.run(
            ["launchctl", "load", "-w", str(LAUNCH_AGENT_PATH)], check=False
        )
    if result.returncode != 0:
        print("launchctl 注册失败, 请手工检查上面的输出", file=sys.stderr)
        return result.returncode
    print(f"已安装 LaunchAgent: {LAUNCH_AGENT_PATH} (日志 {config.log_path}/sidecar.log)")
    return 0


def cmd_uninstall_agent(_args: argparse.Namespace) -> int:
    import subprocess

    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(LAUNCH_AGENT_PATH)], check=False)
    if LAUNCH_AGENT_PATH.exists():
        LAUNCH_AGENT_PATH.unlink()
    print(f"已移除 LaunchAgent: {LAUNCH_AGENT_PATH}")
    return 0


# --------------------------------------------------------------------- 解析


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jev-bridge", description="Rime 万象拼音的 typed-decision 候选重排服务"
    )
    parser.add_argument("--config", help="配置文件路径, 默认 ~/.config/rime-jev/config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="常驻运行: 队列监听 + HTTP 调试端点").set_defaults(func=cmd_serve)
    sub.add_parser("once", help="只处理一次队列里待办的请求").set_defaults(func=cmd_once)

    simulate = sub.add_parser("simulate", help="按 Lua 的格式投递请求并等待响应")
    simulate.add_argument("--code", default="nihao")
    simulate.add_argument("--context", default="")
    simulate.add_argument("--candidates", default="")
    simulate.add_argument("--schema-id", default="wanxiang", dest="schema_id")
    simulate.add_argument("--mode", default="sync", choices=["sync", "prefetch"])
    simulate.add_argument("--timeout-ms", type=int, default=3000, dest="timeout_ms")
    simulate.add_argument(
        "--mock-probabilities", default=None, dest="mock_probabilities", help="形如 0:0.1,1:0.9"
    )
    simulate.add_argument("--mock-error", default=None, dest="mock_error")
    simulate.set_defaults(func=cmd_simulate)

    rerank = sub.add_parser("rerank", help="直接跑一次打分 (不走队列)")
    rerank.add_argument("--request", default="-", help="请求 JSON 文件, 默认读标准输入")
    rerank.set_defaults(func=cmd_rerank)

    bench = sub.add_parser("bench", help="延迟基准 (默认冷缓存, 每次不同上文)")
    bench.add_argument("--count", type=int, default=20)
    bench.add_argument("--code", default="nihao")
    bench.add_argument("--context", default="")
    bench.add_argument("--candidates", default="")
    bench.add_argument("--schema-id", default="wanxiang", dest="schema_id")
    bench.add_argument("--mode", default="sync", choices=["sync", "prefetch"])
    bench.set_defaults(func=cmd_bench)

    sub.add_parser("health", help="查询 HTTP /health").set_defaults(func=cmd_health)
    sub.add_parser("init-config", help="写出默认配置文件").set_defaults(func=cmd_init_config)
    sub.add_parser("show-config", help="打印当前生效配置").set_defaults(func=cmd_show_config)
    logs = sub.add_parser("logs", help="打印 sidecar 日志尾部")
    logs.add_argument("--lines", type=int, default=60)
    logs.set_defaults(func=cmd_logs)
    sub.add_parser("deploy-rime", help="重新部署 Rime (按平台自动选择命令)").set_defaults(
        func=cmd_deploy_rime
    )
    laya_parser = sub.add_parser(
        "laya", help="隔离地启动/停止本机 Laya 后端 (不跑它的 install.sh)"
    )
    laya_parser.add_argument("--port", type=int, default=None)
    laya_parser.add_argument("--python", default=laya.DEFAULT_PYTHON)
    laya_parser.add_argument("--repo", default=laya.DEFAULT_REPO)
    laya_parser.add_argument(
        "--subfolder", default="", help="上游仓库里选 checkpoint, 例如 multilingual"
    )
    laya_parser.add_argument("--background", action="store_true")
    laya_parser.add_argument("--stop", action="store_true")
    laya_parser.add_argument("--status", action="store_true")
    laya_parser.add_argument("--wait-s", type=float, default=600.0, dest="wait_s")
    laya_parser.set_defaults(func=cmd_laya)

    eval_parser = sub.add_parser("eval", help="在 case 集上量候选重排的首选率")
    eval_parser.add_argument(
        "--cases",
        default=str(Path(__file__).resolve().parents[2] / "benchmarks" / "chinese_cases.json"),
    )
    eval_parser.add_argument("--tag", default="")
    eval_parser.add_argument("--repeat", type=int, default=1)
    eval_parser.add_argument("--instructions", default=None)
    eval_parser.add_argument("--fresh", action="store_true", help="先清缓存, 强制走真实后端")
    eval_parser.add_argument("--quiet", action="store_true")
    eval_parser.set_defaults(func=cmd_eval)
    paths_parser = sub.add_parser("paths", help="打印按平台推导的路径并核对两侧 runtime_dir")
    paths_parser.add_argument("--rime-dir", default=None, dest="rime_dir")
    paths_parser.set_defaults(func=cmd_paths)
    sub.add_parser("install-agent", help="安装 LaunchAgent 让 sidecar 常驻 (仅 macOS)").set_defaults(
        func=cmd_install_agent
    )
    sub.add_parser("uninstall-agent", help="卸载 LaunchAgent (仅 macOS)").set_defaults(
        func=cmd_uninstall_agent
    )

    patch = sub.add_parser("patch-config", help="把 jev-rerank 块写入 wanxiang.custom.yaml")
    patch.add_argument("--rime-dir", default=None, dest="rime_dir")
    patch.add_argument("--snippet", default=None)
    patch.add_argument("--dry-run", action="store_true", dest="dry_run")
    patch.add_argument("--show-diff", action="store_true", dest="show_diff")
    patch.set_defaults(func=cmd_patch_config)

    unpatch = sub.add_parser("unpatch-config", help="从 wanxiang.custom.yaml 移除 jev-rerank 块")
    unpatch.add_argument("--rime-dir", default=None, dest="rime_dir")
    unpatch.add_argument("--dry-run", action="store_true", dest="dry_run")
    unpatch.add_argument("--show-diff", action="store_true", dest="show_diff")
    unpatch.set_defaults(func=cmd_unpatch_config)

    sub.add_parser("version", help="打印版本").set_defaults(func=cmd_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
