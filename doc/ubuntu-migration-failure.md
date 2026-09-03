# mcp-ssh Ubuntu 迁移启动失败经验

## 问题现象

从 MacBook 迁移到 Ubuntu 服务器后，`uv sync` 安装依赖失败：

```
× Failed to download `fastmcp-slim==3.4.2`
├─▶ Request failed after 3 retries in 124.4s
├─▶ Failed to fetch: `https://files.pythonhosted.org/.../fastmcp_slim-3.4.2-py3-none-any.whl`
╰─▶ operation timed out
```

## 根因分析

1. **网络环境差异**：MacBook 本地可直接访问 PyPI，Ubuntu 服务器在国内网络环境下访问 PyPI 超时
2. **代理配置缺失**：`uv` 默认不读取系统环境变量 `ALL_PROXY`，需要显式设置 `http_proxy` / `https_proxy`
3. **代理地址错误**：环境变量 `ALL_PROXY=http://172.17.160.1:7890`（Docker 网桥 IP），而非 `127.0.0.1:7890`

## 解决方案

### 1. 清理失败的虚拟环境

```bash
rm -rf .venv
```

### 2. 使用代理重新安装

```bash
export http_proxy=http://172.17.160.1:7890
export https_proxy=http://172.17.160.1:7890
uv sync
```

### 3. 验证启动

```bash
.venv/bin/python mcp_ssh_server.py --help
```

## 永久配置方案（推荐）

### 方案 A：在 pyproject.toml 中配置 uv 索引

```toml
[tool.uv]
index-url = "https://pypi.tuna.tsinghua.edu.cn/simple"
```

或使用代理：

```toml
[tool.uv]
# uv 0.5+ 支持环境变量，可在启动脚本中设置
```

### 方案 B：创建启动脚本

创建 `run.sh`：

```bash
#!/bin/bash
export http_proxy=http://172.17.160.1:7890
export https_proxy=http://172.17.160.1:7890

# 如果虚拟环境不存在，先同步
if [ ! -d ".venv" ]; then
    uv sync
fi

.venv/bin/python mcp_ssh_server.py "$@"
```

### 方案 C：配置国内 PyPI 镜像（无需代理）

```bash
# 临时使用
uv sync --index-url https://pypi.tuna.tsinghua.edu.cn/simple

# 永久配置（写入 pyproject.toml）
cat >> pyproject.toml << 'EOF'

[[tool.uv.index]]
url = "https://pypi.tuna.tsinghua.edu.cn/simple"
default = true
EOF
```

## 关键要点

1. **uv 代理**：`uv` 不自动读取 `ALL_PROXY`，需显式设置 `http_proxy` / `https_proxy`
2. **代理地址**：Docker 容器内访问宿主机代理用 `172.17.160.1:7890`（Docker 网桥 IP），不是 `127.0.0.1`
3. **国内镜像**：清华源 `https://pypi.tuna.tsinghua.edu.cn/simple` 或阿里云源 `https://mirrors.aliyun.com/pypi/simple/` 可避免代理依赖
4. **venv 清理**：安装失败后必须 `rm -rf .venv` 再重试，否则残留状态可能导致后续安装异常

## 验证清单

- [x] 代理连通性：`curl -x http://172.17.160.1:7890 -s -o /dev/null -w "%{http_code}" https://pypi.org/simple/`
- [x] 依赖安装：`uv sync` 成功安装 69 个包
- [x] 服务启动：`python mcp_ssh_server.py --help` 正常输出 FastMCP banner
