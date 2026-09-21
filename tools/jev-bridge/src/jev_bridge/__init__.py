"""jev-bridge: Rime 万象拼音的 typed-decision 候选重排服务端.

职责划分:
- keys.py    跨语言 (Lua / Python) 一致的缓存键与字符串归一化
- config.py  配置文件 (~/.config/rime-jev/config.toml) 读取与校验
- cache.py   磁盘缓存 (TTL + LRU)
- rerank.py  请求校验, 概率到候选顺序的映射与门限
- queue.py   文件队列轮询 (Rime Lua 无法直接连 socket)
- backends/  评分后端 (mock / Jev 兼容 http)
- server.py  本地 HTTP 调试端点
- rime_patch.py  wanxiang.custom.yaml 的 marker 块注入与移除
"""

__version__ = "0.1.0"
