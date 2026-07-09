"""
MCP SSH Server — 通过 expect 脚本连接 JumpServer 堡垒机，支持持久 SSH 会话。

配置: 项目目录下的 config.json，定义多堡垒机 + 多目标资产。
"""

from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OUTPUT_MARKER = "__MCP_OUT__"
DEFAULT_TIMEOUT = 30

# ---------------------------------------------------------------------------
# Config models
# ---------------------------------------------------------------------------


@dataclass
class ServerConfig:
    id: str
    name: str
    search: str
    asset_id: str
    target_dir: str


@dataclass
class BastionConfig:
    id: str
    host: str
    port: int
    user: str
    password: str
    auth_type: str      # "password" | "key"
    key_path: str | None
    proxy: str | None
    default: bool
    servers: list[ServerConfig]


@dataclass
class AppConfig:
    bastions: list[BastionConfig]
    _path: str | None = None
    _mtime: float = 0

    @classmethod
    def load(cls, path: str | Path) -> AppConfig:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        raw = json.loads(path.read_text())

        bastions = []
        for b in raw.get("bastions", []):
            servers = []
            for s in b.get("servers", []):
                servers.append(ServerConfig(
                    id=s["id"],
                    name=s.get("name", s["id"]),
                    search=s["search"],
                    asset_id=str(s.get("asset_id", "1")),
                    target_dir=s["target_dir"],
                ))
            bastions.append(BastionConfig(
                id=b["id"],
                host=b["host"],
                port=b.get("port", 2222),
                user=b["user"],
                password=b.get("password", ""),
                auth_type=b.get("auth_type", "password"),
                key_path=b.get("key_path"),
                proxy=b.get("proxy"),
                default=b.get("default", False),
                servers=servers,
            ))
        return cls(
            bastions=bastions,
            _path=str(path),
            _mtime=path.stat().st_mtime,
        )

    def _reload_if_changed(self):
        """Hot-reload config if the file changed on disk."""
        if not self._path:
            return
        p = Path(self._path)
        if p.stat().st_mtime > self._mtime:
            # Re-read and update in-place
            try:
                fresh = AppConfig.load(p)
                self.bastions = fresh.bastions
                self._mtime = fresh._mtime
                import sys
                print(f"[mcp-ssh] config hot-reloaded from {p.name}", file=sys.stderr)
            except Exception:
                pass  # keep current config if reload fails

    def find_server(self, server_id: str) -> tuple[BastionConfig, ServerConfig]:
        self._reload_if_changed()
        for b in self.bastions:
            for s in b.servers:
                if s.id == server_id:
                    return b, s
        raise KeyError(f"Server '{server_id}' not found in config")

    def list_servers(self) -> list[dict]:
        self._reload_if_changed()
        result = []
        for b in self.bastions:
            for s in b.servers:
                result.append({
                    "bastion": b.id,
                    "bastion_host": b.host,
                    "id": s.id,
                    "name": s.name,
                    "search": s.search,
                    "asset_id": s.asset_id,
                    "target_dir": s.target_dir,
                    "auth_type": b.auth_type,
                })
        return result


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

_config_path = Path(__file__).parent / "config.json"
if _config_path.exists():
    CONFIG = AppConfig.load(_config_path)
else:
    CONFIG = None


# ---------------------------------------------------------------------------
# Expect script template — 热加载
# ---------------------------------------------------------------------------

_expect_template_path = Path(__file__).parent / "expect_template.py"
_expect_template_mtime = 0
_expect_template_module = None


def _build_expect_script(**kwargs) -> str:
    """生成 expect 脚本，expect_template.py 修改后自动重载。"""
    global _expect_template_mtime, _expect_template_module

    # 检查 expect_template.py 是否变更
    if _expect_template_path.exists():
        mtime = _expect_template_path.stat().st_mtime
        if mtime > _expect_template_mtime:
            _expect_template_mtime = mtime
            # 首次加载 / 变更后重载
            spec = importlib.util.spec_from_file_location("expect_template", _expect_template_path)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _expect_template_module = mod
                print(f"[mcp-ssh] expect_template reloaded (mtime={mtime})", file=sys.stderr)

    if _expect_template_module is None:
        raise RuntimeError("expect_template.py not found or failed to load")

    return _expect_template_module.build_expect_script(**kwargs)


# ---------------------------------------------------------------------------
# Session data
# ---------------------------------------------------------------------------


@dataclass
class Session:
    id: str
    process: subprocess.Popen
    status: str  # pending | authenticating | ready | executing | error | closed
    error: str | None
    buffer: str
    lock: threading.Lock
    server_id: str       # which configured server this session is for
    created_at: str
    last_activity: str

    def touch(self):
        self.last_activity = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Session manager
# ---------------------------------------------------------------------------


class SessionManager:
    """Manages expect-based SSH sessions."""

    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._next_id = 1
        self._lock = threading.Lock()

    def connect_from_config(
        self, server_id: str, timeout: int = DEFAULT_TIMEOUT,
    ) -> Session:
        """Connect using a server ID from config.json."""
        if CONFIG is None:
            raise RuntimeError("No config.json found. Use ssh_connect_raw() instead.")
        bastion, server = CONFIG.find_server(server_id)
        return self.connect(
            host=bastion.host, port=bastion.port,
            user=bastion.user, password=bastion.password,
            search=server.search, server_id=server.asset_id,
            target_dir=server.target_dir,
            proxy=bastion.proxy,
            timeout=timeout,
            name=server.id,
            auth_type=bastion.auth_type,
            key_path=bastion.key_path,
        )

    def connect(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        search: str,
        server_id: str = "1",
        target_dir: str = "",
        name: str | None = None,
        proxy: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        auth_type: str = "password",
        key_path: str | None = None,
    ) -> Session:
        """Connect to JumpServer and establish a persistent SSH session."""
        # Ensure search starts with / (only if not empty)
        search_str = f"/{search.lstrip('/')}" if search and not search.startswith("/") else search

        with tempfile.NamedTemporaryFile(mode='w', suffix='.exp', delete=False, prefix='jump_') as tmp:
            script_path = tmp.name
            script = _build_expect_script(
                host=host, port=port, user=user, password=password,
                search=search_str, server_id=server_id, target_dir=target_dir,
                timeout=timeout, proxy=proxy,
                auth_type=auth_type, key_path=key_path,
                marker=OUTPUT_MARKER,
            )
            tmp.write(script)
        os.chmod(script_path, 0o755)

        proc = subprocess.Popen(
            ["expect", "-d", script_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        with self._lock:
            sid = f"session_{self._next_id}"
            self._next_id += 1

        now = datetime.now(timezone.utc).isoformat()
        session = Session(
            id=sid,
            process=proc,
            status="pending",
            error=None,
            buffer="",
            lock=threading.Lock(),
            server_id=name or sid,
            created_at=now,
            last_activity=now,
        )

        t = threading.Thread(
            target=_stdout_reader, args=(session,), daemon=True,
        )
        t.start()

        # 启动 stderr 读取线程用于调试
        def _stderr_reader():
            try:
                while True:
                    line = proc.stderr.readline()
                    if not line:
                        break
                    text = line.decode(errors="replace")
                    print(f"[EXPECT DEBUG] {text}", file=sys.stderr)
            except Exception:
                pass

        t_err = threading.Thread(target=_stderr_reader, daemon=True)
        t_err.start()

        deadline = time.time() + timeout
        while time.time() < deadline:
            with session.lock:
                if session.status == "ready":
                    self._sessions[sid] = session
                    return session
                if session.status == "error":
                    raise RuntimeError(session.error or "Connection failed")
            time.sleep(0.2)

        with session.lock:
            session.status = "error"
            session.error = f"Connection timeout after {timeout}s"
        proc.terminate()
        raise RuntimeError(f"Connection timeout after {timeout}s. Buffer: {session.buffer[-200:]}")

    def execute(self, session_id: str, command: str, timeout_ms: int = 15000) -> str:
        session = self._sessions.get(session_id)
        if not session:
            raise RuntimeError(f"Session {session_id} not found")
        if session.status == "closed":
            raise RuntimeError(f"Session {session_id} is closed")
        if session.status == "error":
            raise RuntimeError(f"Session {session_id} is in error state: {session.error}")

        with session.lock:
            session.status = "executing"
            session.buffer = ""
        session.touch()

        session.process.stdin.write(f"{command}\necho {OUTPUT_MARKER}\n".encode())
        session.process.stdin.flush()

        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            with session.lock:
                if OUTPUT_MARKER in session.buffer:
                    output = session.buffer.split(OUTPUT_MARKER)[0].strip()
                    session.status = "output_ready"
                    session.touch()
                    return output
            time.sleep(0.2)

        with session.lock:
            session.status = "ready"
        return session.buffer.strip() or "(no output, command may still be running)"

    def read_output(self, session_id: str) -> str:
        session = self._sessions.get(session_id)
        if not session:
            raise RuntimeError(f"Session {session_id} not found")
        with session.lock:
            out = session.buffer
            session.buffer = ""
        return out

    def close(self, session_id: str) -> str:
        session = self._sessions.get(session_id)
        if not session:
            return f"Session {session_id} not found"

        try:
            session.process.stdin.write(b"exit\n")
            session.process.stdin.flush()
        except Exception:
            pass

        t0 = time.time()
        while time.time() - t0 < 3:
            if session.process.poll() is not None:
                break
            time.sleep(0.2)
        else:
            session.process.terminate()

        self._sessions.pop(session_id, None)
        with session.lock:
            session.status = "closed"
        return f"Session {session_id} closed"

    def list_sessions(self) -> list[dict]:
        result = []
        for sid, s in self._sessions.items():
            result.append({
                "id": sid,
                "server_id": s.server_id,
                "status": s.status,
                "created": s.created_at,
                "last_activity": s.last_activity,
                "error": s.error,
            })
        return result


# ---------------------------------------------------------------------------
# Stdout reader thread
# ---------------------------------------------------------------------------


def _stdout_reader(session: Session):
    try:
        while True:
            line = session.process.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace")
            with session.lock:
                session.buffer += text
                if session.status == "pending" and "Opt>" in text:
                    session.status = "authenticating"
                if session.status in ("authenticating", "pending") and OUTPUT_MARKER in text:
                    session.status = "ready"
                if session.status in ("authenticating", "pending"):
                    if "Last login" in session.buffer and re.search(r'\$[ \r]*\n?', session.buffer):
                        session.status = "ready"
    except Exception as exc:
        with session.lock:
            session.status = "error"
            session.error = str(exc)


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP("mcp-ssh")
_manager = SessionManager()


@mcp.tool()
async def ssh_connect(
    server_id: Annotated[str, "配置中的服务器 ID（如 dev1-api、dev1-go-mid），见 ssh_list_servers"],
    timeout: Annotated[int, "连接超时秒数"] = DEFAULT_TIMEOUT,
) -> str:
    """通过 config.json 配置连接 JumpServer 堡垒机并建立持久 SSH 会话。"""
    try:
        session = _manager.connect_from_config(server_id, timeout)
        bastion, server = CONFIG.find_server(server_id)
        return (
            f"✅ SSH 连接成功\n\n"
            f"Session ID: {session.id}\n"
            f"Server: {server.name} ({server.id})\n"
            f"Bastion: {bastion.host}\n"
            f"Target Dir: {server.target_dir}\n"
            f"Created: {session.created_at}"
        )
    except Exception as e:
        return f"❌ 连接失败: {e}"


@mcp.tool()
async def ssh_connect_raw(
    host: Annotated[str, "堡垒机地址"] = "service.rrzuji.net",
    port: Annotated[int, "堡垒机端口"] = 2222,
    user: Annotated[str, "堡垒机用户名"] = "xiaolinxin",
    password: Annotated[str, "堡垒机密码"] = "",
    search: Annotated[str, "搜索字符串，如 /39.108.73.45"] = "/39.108.73.45",
    server_id: Annotated[str, "目标资产 ID"] = "1",
    target_dir: Annotated[str, "登录后工作目录"] = "",
    name: Annotated[str | None, "会话名称（可选）"] = None,
    proxy: Annotated[str | None, "SOCKS5 代理地址"] = None,
    timeout: Annotated[int, "连接超时秒数"] = DEFAULT_TIMEOUT,
    auth_type: Annotated[str, "认证方式 (password/key)"] = "password",
    key_path: Annotated[str | None, "SSH 私钥路径（auth_type=key 时使用）"] = None,
) -> str:
    """直接指定参数连接（不依赖 config.json），用于临时连接。"""
    try:
        session = _manager.connect(
            host=host, port=port, user=user, password=password,
            search=search, server_id=server_id, target_dir=target_dir,
            name=name, proxy=proxy, timeout=timeout,
            auth_type=auth_type, key_path=key_path,
        )
        return (
            f"✅ SSH 连接成功\n\n"
            f"Session ID: {session.id}\n"
            f"Host: {host}\n"
            f"User: {user}\n"
            f"Search: {search}\n"
            f"Asset ID: {server_id}\n"
            f"Target Dir: {target_dir}\n"
            f"Created: {session.created_at}"
        )
    except Exception as e:
        return f"❌ 连接失败: {e}"


@mcp.tool()
async def ssh_execute(
    session_id: Annotated[str, "会话 ID（由 ssh_connect 返回）"],
    command: Annotated[str, "要执行的 shell 命令"],
    timeout: Annotated[int, "命令超时毫秒数"] = 15000,
) -> str:
    """在已建立的 SSH 会话中执行命令并返回输出。"""
    try:
        output = _manager.execute(session_id, command, timeout)
        return f"Session: {session_id}\nCommand: {command}\n\n{output}"
    except Exception as e:
        return f"❌ 执行失败: {e}"


@mcp.tool()
async def ssh_read_output(
    session_id: Annotated[str, "会话 ID（由 ssh_connect 返回）"],
) -> str:
    """读取 SSH 会话缓冲区中的最新输出（不发送新命令）。"""
    try:
        output = _manager.read_output(session_id)
        return output or "(no new output)"
    except Exception as e:
        return f"❌ 读取失败: {e}"


@mcp.tool()
async def ssh_close(
    session_id: Annotated[str, "要关闭的会话 ID"],
) -> str:
    """关闭指定的 SSH 会话。"""
    try:
        result = _manager.close(session_id)
        return f"✅ {result}"
    except Exception as e:
        return f"❌ 关闭失败: {e}"


@mcp.tool()
async def ssh_list_sessions() -> str:
    """列出所有当前活跃的 SSH 会话及其状态。"""
    sessions = _manager.list_sessions()
    if not sessions:
        return "没有活跃的 SSH 会话"

    lines = [f"活跃会话列表 ({len(sessions)} 个):\n"]
    for s in sessions:
        lines.append(f"Session: {s['id']}")
        lines.append(f"  Server: {s['server_id']}")
        lines.append(f"  Status: {s['status']}")
        lines.append(f"  Created: {s['created']}")
        lines.append(f"  Last Activity: {s['last_activity']}")
        if s['error']:
            lines.append(f"  Error: {s['error']}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
async def ssh_list_servers() -> str:
    """列出 config.json 中所有可用的服务器配置。"""
    if CONFIG is None:
        return "未找到 config.json"

    servers = CONFIG.list_servers()
    lines = [f"可用的服务器 ({len(servers)} 个):\n"]
    for s in servers:
        lines.append(f"  [{s['id']}] {s['name']}")
        lines.append(f"    堡垒机: {s['bastion']} ({s['bastion_host']})")
        lines.append(f"    搜索: {s['search']}  |  资产 ID: {s['asset_id']}")
        lines.append(f"    目标目录: {s['target_dir']}")
        lines.append(f"    认证: {s['auth_type']}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    mcp.run()


if __name__ == "__main__":
    main()