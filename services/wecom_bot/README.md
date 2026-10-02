# 企业微信智能机器人网关

该服务使用企业微信智能机器人长连接模式，同时处理主动策略推送和用户查询。凭证从仓库根目录 `.env.wecom` 或进程环境变量读取。

首次启动后，在企业微信中给机器人发送：

```text
绑定 <WECOM_BIND_CODE>
```

绑定成功后支持 `今日策略`、`解释 600000`、`状态`、`推送测试` 和 `帮助`。

策略生成程序可通过队列主动推送：

```bash
.venv/bin/python scripts/queue_wecom_strategy.py --file runtime/daily_strategy.md
```

后台服务启动命令：

```bash
scripts/run_wecom_bot.sh
```

当前 macOS 后台运行副本位于 `~/Library/Application Support/abu-wecom-bot`，由 LaunchAgent `com.abu.wecom-bot` 自动启动。修改网关代码后需要把 `services/wecom_bot` 重新同步到该目录。

`.env.wecom`、运行状态、用户绑定和待发送消息均已从 Git 排除。
