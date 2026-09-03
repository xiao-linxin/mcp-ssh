"""Expect 脚本模板 — 可热加载，修改后下次 connect 自动生效。"""


def build_expect_script(
    host, port, user, password, search, server_id, target_dir,
    timeout, proxy, auth_type, key_path, marker,
) -> str:
    """生成 expect 脚本，支持 JumpServer 堡垒机和直连 SSH 两种模式。"""
    # SSH command: key vs password
    ssh_cmd = "spawn ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 -o ServerAliveCountMax=3"
    if auth_type == "key" and key_path:
        ssh_cmd += f' -i "{key_path}"'
    ssh_cmd += " -p $PORT $USER@$HOST"

    # Auth handling
    auth_handlers = []
    if auth_type == "key":
        auth_handlers = [
            '    "Enter passphrase for key" {',
            '        send "$PASS\\r"',
            '    }',
        ]
    else:
        auth_handlers = [
            '    "password:" {',
            '        send "$PASS\\r"',
            '    }',
        ]

    parts = [
        "#!/usr/bin/expect -f",
        "",
        "# log_user 0: 禁止 expect 自动回显，由长连接循环内 send_user 手动输出，避免双重输出",
        "log_user 0",
        "",
        f'set HOST "{host}"',
        f'set PORT "{port}"',
        f'set USER "{user}"',
        f'set PASS "{password}"',
        f'set KEY_PATH "{key_path or ""}"',
        f'set SEARCH "{search}"',
        f'set SERVER_ID "{server_id}"',
        f'set TARGET_DIR "{target_dir}"',
        f'set MARKER "{marker}"',
        f'set timeout {timeout}',
        "",
        "# 连接目标",
        ssh_cmd,
        "",
        "# 处理认证（密码或密钥）— 用短超时，避免密钥免密模式空等30秒",
        "set timeout 5",
        "expect {",
        '    timeout { }',
        '    "yes/no" {',
        '        send "yes\\r"',
        '        exp_continue',
        '    }',
        *auth_handlers,
        "}",
        "set timeout 30",
        "",
    ]

    # 根据 search 是否为空决定连接模式
    if not search or search.strip() == "":
        # 直连模式：等待 shell 就绪（更宽松的模式）
        parts.extend([
            "# 直连模式：等待 shell 就绪",
            "sleep 3",
            "# 发送空命令触发 prompt",
            'send "\\r"',
            "expect -re {[$#]\\s*$}",
            "",
        ])
    else:
        # JumpServer 模式：通过菜单选择资产
        parts.extend([
            "# JumpServer 模式：等待菜单并选择资产",
            'expect "Opt>"',
            'sleep 1',
            'send "$SEARCH\\r"',
            "",
            "# 等待搜索结果，选择资产 ID",
            'expect ">"',
            'send "$SERVER_ID\\r"',
            "",
            "# 等待进入目标服务器 shell",
            "expect -re {\\$ |# }",
            "",
        ])

    # 公共部分：切换目录 + 抑制回显/折行 + 就绪标记 + 长连接
    parts.extend([
        "# 切换工作目录",
        'send "cd $TARGET_DIR\\r"',
        "expect -re {\\$ |# }",
        "",
        "# 抑制命令回显并加宽终端，避免 expect 输出被污染",
        'send "stty -echo 2>/dev/null; stty cols 4096 2>/dev/null\\r"',
        "expect -re {\\$ |# }",
        "",
        "# 输出就绪标记",
        'send_user "$MARKER\\n"',
        "flush stdout",
        "",
        "# 进入长连接模式：expect_user 处理 stdin，后台监听 spawn 输出",
        "set timeout 1",
        "while {1} {",
        '    expect_user -re "(.*)\\n" {',
        '        send "$expect_out(1,string)\\r"',
        "    }",
        "    # 检查 spawn 是否有输出",
        '    expect -timeout 0 -i $spawn_id -re "(.+)" {',
        '        send_user "$expect_out(0,string)"',
        "        flush stdout",
        "    }",
        "}",
    ])

    return "\n".join(parts)
