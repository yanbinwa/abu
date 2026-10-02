# 企业微信群策略日报发送

`send_wecom_strategy.py` 只负责发送已经生成的日报文本。当前项目的回测脚本使用历史数据，不能直接作为当天交易指令；等确定策略、股票池和运行时间后，再把相应的日报生成命令接到发送命令前面。

## 获取 Webhook

在目标企业微信群中创建“消息推送/自定义消息推送”，复制该群的 Webhook 地址。地址包含密钥，请只放在运行环境变量中，不要写入仓库或截图公开。

```bash
export WECOM_WEBHOOK_URL='https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的密钥'
```

## 先校验，再发送

把当天的策略日报写入 UTF-8 文本文件（最多 2048 字节）：

```bash
.venv/bin/python scripts/send_wecom_strategy.py --file /path/to/today_strategy.txt --dry-run
.venv/bin/python scripts/send_wecom_strategy.py --file /path/to/today_strategy.txt
```

也可由日报生成程序经标准输入传递：

```bash
python your_daily_strategy.py | .venv/bin/python scripts/send_wecom_strategy.py --stdin
```

发送程序会校验 HTTP 请求和企业微信返回的 `errcode`。空内容、超长内容、无效 Webhook 或接口报错时，命令以非零状态退出。使用计划任务时，应先运行并确认日报生成成功，再调用发送命令；避免重复运行或把旧日报重新发送。

企业微信文档：[消息推送配置说明](https://developer.work.weixin.qq.com/document/path/99110)。
