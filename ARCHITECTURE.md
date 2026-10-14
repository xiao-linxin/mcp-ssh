# mcp-ssh 架构文档

> 一个基于 expect + uvx + Python MCP 的 JumpServer 堡垒机 SSH 持久连接方案。
> 解决传统 SSH MCP 工具无法通过交互式堡垒机保持长连接的问题。

---

## 一、架构总览

```
┌──────────────────────────────────────────────────────────────────────┐
│                         Claude Code (MCP Client)                     │
│                            调用 MCP 工具                              │
└──────────────────────────┬───────────────────────────────────────────┘
                           │ stdio JSON-RPC
                           ▼
┌──────────────────────────────────────────────────────────────────────┐
│                      MCP 服务进程 (uvx mcp-ssh)                      │
│                                                                      │
│  ┌──────────────────────┐    ┌──────────────────────────────────┐   │
│  │   mcp_ssh_server.py   │    │        config.json (热加载)       │   │
│  │                      │    │                                  │   │
│  │  SessionManager      │◄──►│  bastions[]                      │   │
│  │  ├─ connect()        │    │  └─ servers[]:                   │   │
│  │  ├─ execute()        │    │     dev1-api, dev1-go-mid, ...   │   │
│  │  ├─ close()          │    └──────────────────────────────────┘   │
│  │  └─ list_sessions()  │                                           │
│  └────────┬─────────────┘                                           │
│           │ spawn + pipe                                             │
│           ▼                                                          │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │               expect_template.py (热加载)                      │   │
│  │                                                                │   │
│  │  生成 expect 脚本:                                              │   │
│  │  1. ssh -i key → JumpServer 堡垒机                               │   │
│  │  2. 菜单交互 (Opt> → p → 搜索 → 选资产)                          │   │
│  │  3. 进入目标服务器 shell                                          │   │
│  │  4. while 长连接循环                                             │   │
│  └────────┬─────────────────────────────────────────────────────────┘
│           │ spawn expect
│           ▼
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │              expect 进程 (每个 session 一个)                     │   │
│  │                                                                │   │
│  │  ┌─────────────────────┐                                       │   │
│  │  │  spawn ssh          │──→ JumpServer → 目标服务器              │   │
│  │  │  stdin pipe ◀───────│── 接收 Python 发来的命令                │   │
│  │  │  stdout pipe ──────▶│── 输出到 Python reader 线程             │   │
│  │  │  while 循环         │── 双通道转发                            │   │
│  │  └─────────────────────┘                                       │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

### 核心思维：管道接力

```
Claude Code  ←──stdin/stdout──→  Python MCP
                                      │
                                stdin pipe │ stdout pipe
                                      │
                                  expect 进程
                                      │
                                ssh -i key → JumpServer → 目标服务器
```

每一层之间的通信都是 **管道（pipe）**，不是文件、不是网络。数据流：

1. **stdin 管道**：Claude → Python → expect → SSH → 目标服务器 Shell
2. **stdout 管道**：目标服务器 Shell → SSH → expect → Python → Claude

---

## 二、项目文件结构

```
a-mcp/mcp-ssh/
├── mcp_ssh_server.py        # MCP 服务主入口 + SessionManager
├── expect_template.py       # expect 脚本模板（热加载）
├── config.json              # 多堡垒机 + 多资产配置（热加载）
├── config.example.json      # 配置示例
├── pyproject.toml           # Python 项目配置 / uv 入口
└── uv.lock                  # 依赖锁定
```

---

## 三、功能清单

### 1. 多堡垒机 + 多资产配置

**能力**：一个 config.json 管理多台 JumpServer 和多台目标服务器。

**代码位置**：

```
config.json                    # 配置文件
mcp_ssh_server.py:58-141      # AppConfig 数据模型 + 加载
mcp_ssh_server.py:100-114     # _reload_if_changed() 热加载逻辑
mcp_ssh_server.py:562-577     # ssh_list_servers() 列出服务器
```

**示例配置**（`config.json`）：

```json
{
  "bastions": [
    {
      "id": "jump-dev",
      "host": "service.rrzuji.net",
      "port": 2222,
      "user": "xiaolinxin",
      "auth_type": "key",
      "key_path": "/path/to/key.pem",
      "servers": [
        {
          "id": "dev1-api",
          "name": "API 服务",
          "search": "/39.108.73.45",
          "asset_id": "1",
          "target_dir": "/k8s-alidata/www/api/api19"
        }
      ]
    }
  ]
}
```

**思维**：一个 bastion 对应一台 JumpServer 堡垒机，一个 server 对应堡垒机上的一台目标资产。每个 server 有唯一 `id`，通过 `ssh_connect(server_id="dev1-api")` 连接。

---

### 2. SSH 持久长连接

**能力**：每个连接对应一个独立 expect 子进程，长期存活。执行命令不反复鉴权。

**代码位置**：

```
mcp_ssh_server.py:193-205     # Session 数据模型
mcp_ssh_server.py:207-211     # SessionManager 类
mcp_ssh_server.py:254-340     # connect() 方法
mcp_ssh_server.py:342-372     # execute() 方法
expect_template.py:82-93      # while 长连接循环
```

**思维**：

```
Session 数据结构:
┌─────────────────────────────────────┐
│ id: session_1                        │
│ process: expect 子进程 (Popen)        │
│ buffer: string (stdout 累积)          │
│ status: pending→ready→executing→...  │
│ lock: threading.Lock (线程安全)       │
│ cond: Condition(lock) (等待/唤醒)     │
│ exec_lock: Lock (同会话命令串行)       │
│ seq: int (命令序号 → 唯一输出标记)     │
│ server_id: dev1-api-49 (所属服务器)    │
│ created_at / last_activity            │
└─────────────────────────────────────┘
```

**长连接 while 循环**（`expect_template.py`）：

```tcl
while {1} {
    # 阻塞等待 stdin 输入（来自 Python execute 方法）
    expect_user -re "(.*)\n" {
        send "$expect_out(1,string)\r"   # 转发到 SSH
    }
    # 非阻塞检查 SSH 输出
    expect -timeout 0 -i $spawn_id -re "(.+)" {
        send_user "$expect_out(0,string)"  # 转发到 stdout
        flush stdout
    }
}
```

**为什么这个循环能工作？**
- `expect_user` 阻塞等待 Python 发来的命令
- 收到命令后 `send` 发送到 SSH（目标服务器 shell）
- SSH 执行命令产生输出，被 `expect -i $spawn_id` 非阻塞捕获
- 用 `send_user` 显式写入 stdout 管道（**关键**：`log_user` 写入的是日志文件，不是 stdout 管道，必须用 `send_user`）

---

### 3. 并发执行（多 session / 多 AI 对话互不阻塞）

**能力**：多个 AI 对话同时使用（同一个 MCP 进程）时，各自独立、互不阻塞。

**两级并发模型**：

| 层级 | 隔离单位 | 机制 |
|------|---------|------|
| 跨会话（不同 AI 对话） | `session_id` | 每个 session 一个独立的 expect 子进程 + SSH 连接，命令互不影响；工具函数在线程池中执行，不阻塞事件循环 |
| 会话内（同一 session 上并发调用） | `Session.exec_lock` | 同一会话的命令**串行**执行，配合每次命令唯一标记 `__MCP_OUT__{seq}_`，保证输出不串台 |

**思维**：每个 `server_id` → 一次 `ssh_connect` → 一个 expect 子进程 → 一个 SSH 连接（即一个"通道"）。

```
SessionManager._sessions = {
    session_1: { process: expect_pid_123, server_id: "dev1-api-49" },   ← 对话 A
    session_2: { process: expect_pid_456, server_id: "dev1-api-49" },   ← 对话 B
}
```

同一台目标服务器被两个对话使用时，**各自连一个 session**（而不是共用一个 session 抢同一个 shell），
因此 `cd`、环境变量等 shell 状态互不污染；代价是每个对话各占一条堡垒机连接。

> 结论：`session_id` 本身就是并发隔离的"管道"，不需要再额外引入 channel 概念。

**实测**（真实堡垒机，`sleep 6` 与 `pwd` 并发）：

```
会话1: sleep 6; echo SLOW_DONE
会话2: pwd                      → 1.0s 返回（未被 sleep 6 阻塞）
同会话并发 echo AAA / echo BBB   → 各自拿到自己的输出
```

---

### 4. 热加载（不重启改配置和模板）

**能力**：改 `config.json` 或 `expect_template.py`，**不需要重启 Claude**，下次调用自动生效。

**代码位置**：

```
# config.json 热加载
mcp_ssh_server.py:100-114     # _reload_if_changed()
mcp_ssh_server.py:116-120     # find_server / list_servers 调用

# expect_template.py 热加载
mcp_ssh_server.py:124-148     # _build_expect_script()
mcp_ssh_server.py:127-140     # 检查 mtime + importlib.reload()
```

**思维**：每次查配置或生成 expect 脚本前，检查文件的 `st_mtime`（修改时间）。变了就重读。

```python
def _reload_if_changed(self):
    p = Path(self._path)
    if p.stat().st_mtime > self._mtime:
        fresh = AppConfig.load(p)            # 重新加载
        self.bastions = fresh.bastions        # 替换配置
        self._mtime = fresh._mtime            # 更新缓存时间
```

---

### 5. 密钥 + 密码双认证

**能力**：支持 SSH 密钥（`auth_type: "key"`）和密码（`auth_type: "password"`）两种方式登录堡垒机。

**代码位置**：

```
expect_template.py:22-27     # ssh_cmd 构建（key 时加 -i 参数）
expect_template.py:29-42     # auth_handlers 区分 key/password
expect_template.py:49-55     # set timeout 5 短超时（key 免密不等30秒）
config.json:8-10             # auth_type + key_path 配置
```

**思维**：`auth_type` 控制 expect 脚本的行为：
- **key 模式**：`ssh -i key_path user@host`，密码段跳过，只处理 `yes/no` 和 `passphrase`
- **password 模式**：`ssh user@host`，处理 `password:` 输入

**关键细节**：key 免密模式必须用 `set timeout 5` 短超时，否则 `expect { timeout { } }` 会等待 30 秒。

---

### 6. 快速命令执行（marker 分割）

**能力**：执行命令后立即返回输出，不等待 SSH 空闲。

**代码位置**：

```
mcp_ssh_server.py  execute()   # 命令执行 + 唯一标记检测
mcp_ssh_server.py  OUTPUT_MARKER = "__MCP_OUT__"    # 命令标记前缀
mcp_ssh_server.py  READY_MARKER  = "__MCP_READY__"  # 连接就绪标记（避免与命令标记混淆）
```

**思维**：发送命令时，在后面追加 `echo __MCP_OUT__{seq}_`（`seq` 为该会话的命令序号）。
输出中出现本次自己的标记时，标记之前的内容就是命令输出。用递增序号而不是固定标记，
是为了防止"上一条超时命令的残留标记"被当成本次结果。

```python
session.seq += 1
marker = f"{OUTPUT_MARKER}{session.seq}_"        # __MCP_OUT__3_
session.process.stdin.write(f"date\necho {marker}\n".encode())
# 等待条件变量被唤醒，直到发现 marker
# 按标记分割：buffer.split(marker, 1)[0]
```

---

## 四、核心技术决策

### 为什么用 expect 而不是 ssh2 库？

JumpServer 堡垒机是**交互式菜单程序**，不是标准 SSH 跳板机。它禁止 `ProxyJump` 和端口转发，只能通过模拟键盘输入来操作。`ssh2` 等 Node.js 库无法处理这种场景，`expect` 是唯一可靠的方式。

### 为什么用 send_user 而不是 log_user？

| 函数 | 写入目标 | 是否可达 Python reader 线程 |
|------|---------|:------------------------:|
| `send_user` | stdout 管道 | ✅ 可达 |
| `log_user` | 日志文件（不是 stdout） | ❌ 不可达 |

`log_user 1` 的输出不会写入 stdout 管道，Python 读不到。必须用 `send_user` + `flush stdout` 显式转发。

### 为什么 Python 需要 reader 线程？

expect 进程的 stdout 是管道，如果不持续读取，管道缓冲区满了会导致 expect 阻塞（即死锁）。reader 线程用 `readline()` 持续读取，写入 session 的 `buffer` 字段。

```python
def _stdout_reader(session):
    while True:
        line = session.process.stdout.readline()
        session.buffer += line.decode()
        # 状态检测：是否出现标记、是否连接就绪
```

### 为什么 stderr 要 DEVNULL？

expect 脚本和 SSH 都可能写 stderr 内容。如果不读取 stderr 管道，它也会满，导致 expect 进程阻塞。直接用 `stderr=subprocess.DEVNULL` 丢弃。

### 为什么多个 AI 对话并发时会一直卡住？（三个坑）

MCP 服务端对每个请求是**并发派发**的（`mcp/server/lowlevel/server.py` 里 `tg.start_soon(self._handle_message, ...)`），
所以"卡住"不是协议层的问题，而是实现层的三个坑：

| # | 坑 | 后果 | 修法 |
|:-:|----|------|------|
| 1 | 工具写成 `async def`，内部却是 `time.sleep()` 轮询 | FastMCP 对 **async 工具直接 `await`**（不走线程池），整个事件循环被挂住 → 所有对话的请求排队等待 | 工具改为**同步 `def`**；FastMCP 默认 `run_in_thread=True`，会放进线程池执行 |
| 2 | 同一个 session 被并发 `execute`：双方都在开头 `buffer = ""` | 先到的那条命令的输出标记被后到的清掉 → 永远等不到标记，一直等到超时 | `Session.exec_lock` 串行化同一会话的命令 |
| 3 | `OUTPUT_MARKER` 全局固定 + 轮询 `sleep(0.2)` | 上一条超时命令的残留标记被误判为本次结果；expect 进程退出后等待方要空等满超时 | 每次命令用唯一标记 `__MCP_OUT__{seq}_`；`threading.Condition` 通知取代轮询，进程 EOF / close 立刻置错唤醒 |

> ⚠️ 因此**不要**把 `mcp_ssh_server.py` 里的工具函数改回 `async def`，
> 除非同时用 `anyio.to_thread.run_sync(...)` 把阻塞逻辑丢进线程池。

---

## 五、数据流详解

### 连接流程

```
connect("dev1-api")
  │
  ├─ AppConfig.find_server("dev1-api")
  │   └─ 查 config.json → bastion + server 配置
  │
  ├─ _build_expect_script(...)
  │   └─ 加载 expect_template.py（热加载）
  │   └─ 生成 expect 脚本 → 写入 tmp 文件
  │
  ├─ subprocess.Popen(["expect", script_path])
  │   └─ stdin=PIPE, stdout=PIPE, stderr=DEVNULL
  │
  ├─ _stdout_reader 线程启动
  │   └─ while: readline() → buffer += line
  │
  ├─ expect 脚本执行:
  │   1. spawn ssh -i KEY USER@HOST
  │   2. expect { yes/no | password: | passphrase }
  │     → auth_type=key: set timeout 5 避免空等
  │   3. expect "Opt>" → 菜单出现
  │   4. send "$SEARCH\r" → 搜索目标资产
  │   5. expect ">" → 选资产
  │   6. expect -re {\$ |# } → shell 提示符
  │   7. send "cd $TARGET_DIR\r" → 切换目录
  │   8. send_user "__MCP_OUT__\n" → 标记就绪
  │   9. enter while 长连接循环
  │
  ├─ reader 线程检测到 "__MCP_OUT__"
  │   └─ session.status = "ready"
  │   └─ resolve(session) → connect 返回
  │
  └─ 返回 ✅ SSH 连接成功
```

### 命令执行流程

```
execute(session_1, "pwd")
  │
  ├─ session.status = "executing"
  ├─ session.buffer = ""         ← 清空缓冲区
  │
  ├─ exec_lock.acquire()          ← 同一会话内命令串行
  ├─ seq += 1 → marker="__MCP_OUT__4_"
  ├─ stdin.write("pwd\necho __MCP_OUT__4_\n")
  │   └─ expect 进程收到 stdin
  │
  ├─ expect_user -re "(.*)\n"
  │   ├─ 匹配 "pwd"
  │   └─ send "pwd\r" → SSH → 目标服务器
  │
  ├─ expect -timeout 0 -i $spawn_id
  │   ├─ 匹配 "pwd\r\n/xxx/api19\r\n"
  │   └─ send_user → stdout pipe
  │
  ├─ reader 线程: readline() → buffer += "pwd\r\n/xxx/api19\r\n"
  │
  ├─ expect_user 再匹配 "echo __MCP_OUT__4_"
  │   └─ send "echo __MCP_OUT__4_\r" → SSH
  │
  ├─ spawn 输出 "__MCP_OUT__4_\r\n"
  │   └─ reader 线程 buffer += 该行，并 notify() 唤醒等待方
  │
  ├─ execute 被唤醒，检测到本次自己的 marker
  │   └─ split(marker, 1)[0] → "pwd\r\n/xxx/api19\r\n"
  │   └─ 清空 buffer（本次输出已消费）→ status=ready
  │   └─ exec_lock.release()
  │   └─ return output
  │
  └─ 返回命令输出
```

### 关闭流程

```
close(session_1)
  │
  ├─ status="closed" + notify()   ← 立刻唤醒正在等待输出的 execute
  ├─ stdin.write("exit\n")        ← 优雅退出
  ├─ 等待 3 秒
  ├─ 如果进程没退出 → terminate()
  ├─ _sessions.pop(session_1)
  └─ 返回 ✅ Session closed
```

---

## 六、关键代码索引

| 功能 | 文件 | 行号 | 说明 |
|------|------|:----:|------|
| MCP 服务器入口 | `mcp_ssh_server.py` | 1-25 | imports, 常量, FastMCP 初始化 |
| 配置模型 | `mcp_ssh_server.py` | 32-56 | ServerConfig, BastionConfig, AppConfig |
| 配置热加载 | `mcp_ssh_server.py` | 100-114 | `_reload_if_changed()` |
| session 数据模型 | `mcp_ssh_server.py` | 186-205 | Session dataclass |
| 连接方法 | `mcp_ssh_server.py` | 251-340 | `connect()` + `connect_from_config()` |
| 命令执行 | `mcp_ssh_server.py` | 342-372 | `execute()` + marker 检测 |
| 关闭方法 | `mcp_ssh_server.py` | 374-398 | `close()` |
| reader 线程 | `mcp_ssh_server.py` | 406-427 | `_stdout_reader()` |
| MCP 工具注册 | `mcp_ssh_server.py` | 437-577 | FastMCP `@mcp.tool()` 装饰器 |
| 入口函数 | `mcp_ssh_server.py` | 581-584 | `main()` |
| expect 模板（热加载）| `expect_template.py` | 全文件 | `build_expect_script()` |
| while 长连接循环 | `expect_template.py` | 82-93 | 双通道转发 |
| 配置（多堡垒机）| `config.json` | 全文件 | bastions[] + servers[] |

---

## 七、常见问题

### Q: 一个 bastion 可以多个 key 吗？

目前 `auth_type` 和 `key_path` 在 bastion 级别。如果需要同一个堡垒机不同服务器用不同密钥，可以拆成两个 bastion。

### Q: expect 脚本怎么调试？

1. 修改 `expect_template.py`，在关键步骤前加 `send_user "DEBUG: xxx\n"`
2. 热加载自动生效，下次 `ssh_connect` 会包含调试输出

### Q: 连接断了怎么办？

会话监控：`ssh_list_sessions` 看状态。如果状态是 `closed` 或 `error`，重连即可。

### Q: 为什么输出有 ANSI 乱码？

SSH 通过 PTY 传输，会带终端控制字符。这是正常的，命令输出本身不受影响（乱码都是命令回显和 prompt）。

---

## 八、开发指南

### 本地启动测试

```bash
cd /Users/yoko/www/xpro/a-mcp/mcp-ssh
uv run python mcp_ssh_server.py
```

### 修改 expect 行为

改 `expect_template.py` 的 `build_expect_script()` 函数，改完即时生效。

### 添加新服务器

在 `config.json` 的 `servers` 数组中加条目，热加载自动生效。

```json
{
  "id": "dev1-xxx",
  "name": "新服务",
  "search": "/39.108.73.45",
  "asset_id": "1",
  "target_dir": "/path/to/dir"
}
```

### 添加新 MCP 工具

在 `mcp_ssh_server.py` 用 `@mcp.tool()` 装饰器加函数：

```python
@mcp.tool()
async def ssh_my_new_tool(param: str) -> str:
    """工具描述。"""
    # 实现逻辑
    return "结果"
```