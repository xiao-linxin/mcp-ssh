# MCP SSH Server

一个基于 **expect + Python MCP** 的 SSH 持久连接方案，专为 JumpServer 堡垒机设计。

解决传统 SSH MCP 工具无法通过交互式堡垒机保持长连接的问题。

## 特性

- **多堡垒机支持** - 一个配置文件管理多台 JumpServer 和多台目标服务器
- **持久长连接** - 每个连接对应独立 expect 子进程，长期存活无需反复鉴权
- **并发执行** - 同时连接多台服务器，各自独立互不干扰
- **热加载** - 修改配置或模板后无需重启，下次调用自动生效
- **双认证模式** - 支持 SSH 密钥和密码两种认证方式
- **快速响应** - 使用 marker 分割技术，命令执行后立即返回输出

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│                    Claude Code / MCP Client                  │
└──────────────────────────┬──────────────────────────────────┘
                           │ stdio JSON-RPC
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                   MCP SSH Server (Python)                    │
│  ┌──────────────────┐    ┌──────────────────────────────┐  │
│  │ SessionManager   │    │   config.json (热加载)        │  │
│  │ ├─ connect()     │◄──►│   bastions[] → servers[]     │  │
│  │ ├─ execute()     │    └──────────────────────────────┘  │
│  │ └─ close()       │                                       │
│  └────────┬─────────┘                                       │
│           │ spawn + pipe                                     │
│           ▼                                                  │
│  ┌──────────────────────────────────────────────────────┐   │
│  │         expect_template.py (热加载)                    │   │
│  │         生成 expect 脚本处理交互式认证                  │   │
│  └────────┬─────────────────────────────────────────────┘   │
└───────────┼──────────────────────────────────────────────────┘
            │ spawn expect
            ▼
┌─────────────────────────────────────────────────────────────┐
│                    expect 进程 (per session)                  │
│  ┌─────────────────┐                                        │
│  │ spawn ssh       │──→ JumpServer → 目标服务器              │
│  │ stdin pipe      │◄── 接收 Python 发来的命令               │
│  │ stdout pipe     │── 输出到 Python reader 线程             │
│  └─────────────────┘                                        │
└─────────────────────────────────────────────────────────────┘
```

## 安装

### 前置要求

- Python 3.10+
- expect (系统命令)
- [uv](https://github.com/astral-sh/uv) (推荐的 Python 包管理器)

### 安装 expect

**macOS:**
```bash
brew install expect
```

**Ubuntu/Debian:**
```bash
sudo apt-get install expect
```

**CentOS/RHEL:**
```bash
sudo yum install expect
```

### 安装项目

```bash
cd a-mcp/mcp-ssh
uv sync
```

## 配置

复制配置示例并修改：

```bash
cp config.example.json config.json
```

### 配置示例

```json
{
  "bastions": [
    {
      "id": "bastion-01",
      "host": "bastion.example.com",
      "port": 22,
      "user": "your_username",
      "auth_type": "key",
      "key_path": "~/.ssh/your_key.pem",
      "password": "",
      "default": true,
      "servers": [
        {
          "id": "server-01",
          "name": "应用服务器 1",
          "search": "/192.168.1.100",
          "asset_id": "1",
          "target_dir": "/var/www/app1"
        },
        {
          "id": "server-02",
          "name": "应用服务器 2",
          "search": "/192.168.1.101",
          "asset_id": "2",
          "target_dir": "/var/www/app2"
        }
      ]
    },
    {
      "id": "direct-server",
      "host": "direct.example.com",
      "port": 22,
      "user": "your_username",
      "auth_type": "password",
      "key_path": "",
      "password": "your_password",
      "default": false,
      "servers": []
    }
  ]
}
```

### 配置说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 唯一标识符 |
| `host` | string | 服务器地址 |
| `port` | int | SSH 端口（默认 22） |
| `user` | string | 用户名 |
| `auth_type` | string | 认证方式：`key` 或 `password` |
| `key_path` | string | SSH 私钥路径（`auth_type=key` 时必填） |
| `password` | string | 密码（`auth_type=password` 时必填） |
| `default` | bool | 是否为默认堡垒机 |
| `servers` | array | 该堡垒机下的目标服务器列表 |

**服务器配置 (`servers[]`):**

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 服务器唯一 ID（用于 `ssh_connect`） |
| `name` | string | 服务器名称（描述用） |
| `search` | string | JumpServer 搜索关键词（如 `/192.168.1.100`） |
| `asset_id` | string | JumpServer 资产 ID |
| `target_dir` | string | 登录后切换的工作目录 |

## 使用方法

### 作为 MCP 服务

在 Claude Code 或其他 MCP 客户端中配置：

```json
{
  "mcpServers": {
    "ssh": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/mcp-ssh", "python", "mcp_ssh_server.py"]
    }
  }
}
```

### MCP 工具列表

| 工具 | 说明 |
|------|------|
| `ssh_connect` | 通过配置连接服务器（推荐） |
| `ssh_connect_raw` | 直接指定参数连接（临时使用） |
| `ssh_execute` | 在会话中执行命令 |
| `ssh_read_output` | 读取会话缓冲区输出 |
| `ssh_close` | 关闭会话 |
| `ssh_list_sessions` | 列出所有活跃会话 |
| `ssh_list_servers` | 列出配置中的所有服务器 |

### 使用示例

#### 1. 连接服务器

```python
# 使用配置中的服务器 ID
ssh_connect(server_id="server-01")

# 或直接指定参数
ssh_connect_raw(
    host="example.com",
    port=22,
    user="admin",
    password="secret",
    search="",  # 直连模式
    target_dir="/home/admin"
)
```

#### 2. 执行命令

```python
ssh_execute(session_id="session_1", command="ls -la")
ssh_execute(session_id="session_1", command="df -h")
```

#### 3. 管理会话

```python
# 查看所有活跃会话
ssh_list_sessions()

# 关闭指定会话
ssh_close(session_id="session_1")
```

## 项目结构

```
mcp-ssh/
├── mcp_ssh_server.py        # MCP 服务主入口 + SessionManager
├── expect_template.py       # expect 脚本模板（支持热加载）
├── config.json              # 多堡垒机 + 多资产配置（需自行创建）
├── config.example.json      # 配置示例
├── pyproject.toml           # Python 项目配置
├── uv.lock                  # 依赖锁定
├── ARCHITECTURE.md          # 详细架构文档
└── README.md                # 本文件
```

## 开发指南

### 本地测试

```bash
cd a-mcp/mcp-ssh
uv run python mcp_ssh_server.py
```

### 热加载机制

- **config.json** - 每次调用 `ssh_connect` 或 `ssh_list_servers` 时检查修改时间，变更则重新加载
- **expect_template.py** - 每次生成 expect 脚本时检查修改时间，变更则重新加载模块

修改后无需重启服务，下次调用自动生效。

### 添加新服务器

在 `config.json` 的 `servers` 数组中添加条目：

```json
{
  "id": "new-server",
  "name": "新服务器",
  "search": "/10.0.0.100",
  "asset_id": "1",
  "target_dir": "/opt/app"
}
```

### 修改 expect 行为

编辑 `expect_template.py` 的 `build_expect_script()` 函数，修改后即时生效。

## 常见问题

### Q: 为什么用 expect 而不是 ssh2 库？

JumpServer 堡垒机是**交互式菜单程序**，不是标准 SSH 跳板机。它禁止 `ProxyJump` 和端口转发，只能通过模拟键盘输入来操作。`expect` 是处理这种场景的最可靠方式。

### Q: 连接断了怎么办？

使用 `ssh_list_sessions` 查看会话状态。如果状态是 `closed` 或 `error`，重新调用 `ssh_connect` 即可。

### Q: 输出有 ANSI 乱码？

SSH 通过 PTY 传输，会带终端控制字符。这是正常的，命令输出本身不受影响。

### Q: 如何调试 expect 脚本？

在 `expect_template.py` 的关键步骤前添加：

```python
'send_user "DEBUG: 当前步骤\\n"',
"flush stdout",
```

热加载会自动生效。

## 许可证

MIT

## 贡献

欢迎提交 Issue 和 Pull Request！
