# Telegram 信号跟单机器人

实时监听 Telegram 频道的交易信号，自动在交易所（Binance / OKX / Bybit / Bitget / Gate，USDT 永续合约）下单，带止损、止盈、风控和模拟盘统计。

默认监听：
[IvanCryptotalk](https://t.me/IvanCryptotalk) ·
[zxcccckhf](https://t.me/zxcccckhf) ·
[chun77chun](https://t.me/chun77chun) ·
[yuyanjia66](https://t.me/yuyanjia66)

## 工作流程

```
Telegram 频道新消息
   └─► 解析器（规则解析，可选 Claude 兜底）──► 不是信号 → 忽略
          └─► 去重 / 消息时效检查
                 └─► 风控：必须有止损、杠杆上限、自动降杠杆防爆仓、仓位大小、
                     最大持仓数、每日次数、每日亏损上限、追价保护
                        └─► 下单：市价/限价开仓 + 止损单 + 分批止盈单（全部 reduce-only）
                               └─► 通知发到你的 Telegram「收藏夹」
后台每 30 秒：检测止盈止损是否成交、第一止盈后止损移到保本、清理残留挂单、模拟盘撮合
```

能识别的格式（基于这 4 个频道的真实消息测试，见 `tests/test_parser.py`）：

| 频道 | 示例 |
|---|---|
| IvanCryptotalk | `BTC（100X做多📈）進場：限價81900—80999 止盈：84438—86076—90171 離場：79443` |
| zxcccckhf | `#TIA 輕倉市價空 進場 : 0.4436 ✅止盈：0.4311-0.4134 ❌止損：0.4601` |
| chun77chun | `#WIF 輕倉市價空 止盈：0.2441 止損：0.2649` |
| yuyanjia66 | `#ETH （100x做空）進場位： 2778 盈利位：2700—2600 止損位：2830` |
| 后续指令 | `$COTI 提損0.01553`（移止损）、`減倉30%`、`全部平倉` |

广告、晒单、"今天想做空這個" 之类的闲聊会被忽略。

## ⚠️ 上线前请务必看完

1. **先跑模拟盘。** `dry_run: true` 时机器人会按真实行情模拟成交、止盈、止损，并统计每个频道的胜率和盈亏（发 `/status` 查看）。建议至少跑 1–2 周，用数据决定跟哪个频道、跟多少钱。
2. **这几个频道本质是付费群的引流频道**（大量"帶單落袋""限時免費進群""聯繫助理"的广告），公开频道里放出的信号是挑选过的"体验单"，晒出来的战绩无法核实。很多信号**没有止损**（如 `$COTI 50X做多`、`#DYM 市價輕倉多`），默认配置会**跳过没有止损的信号**。
3. **不会用信号里的 100x 杠杆。** 默认最大 10x，并且会自动降杠杆，保证先打止损、不会先爆仓。
4. **交易所 API 只开合约交易权限，关闭提现权限**，并绑定 DigitalOcean 服务器 IP 白名单。
5. 合约账户需是**单向持仓模式**（One-way mode），不支持双向持仓。
6. 机器人只管理它自己开的仓位；你手动开的仓不会被动。

## 部署到 DigitalOcean

### 1. 准备

- **Telegram API**：登录 <https://my.telegram.org> → API development tools → 创建应用，拿到 `api_id` 和 `api_hash`。
  （必须用**用户账号**而不是 Bot，因为 Bot 无法读取别人的频道。建议用小号。）
- **交易所 API Key**：只勾选合约交易，IP 白名单填你的 Droplet IP。

### 2. 安装

```bash
ssh root@你的服务器IP
apt update && apt install -y git docker.io docker-compose-v2
git clone <本仓库地址> /opt/tgtrader && cd /opt/tgtrader

cp .env.example .env && chmod 600 .env
cp config.example.yaml config.yaml
nano .env          # 填 TG_API_ID / TG_API_HASH / TG_PHONE / 交易所 key
nano config.yaml   # 调整频道、仓位、风控（默认 dry_run: true）
```

### 3. 首次登录 Telegram（只需一次，会收到验证码）

```bash
mkdir -p data
docker compose run --rm tgtrader python -m tgtrader.main --login
```

session 保存在 `data/tg.session`，**不要泄露这个文件**（等同于你的 Telegram 登录状态）。

### 4. 启动

```bash
docker compose up -d --build
docker compose logs -f          # 看日志
```

启动后你的 Telegram「收藏夹 / Saved Messages」会收到 `🤖 跟单机器人已启动`。

### 5. 转实盘

模拟盘数据满意后，把 `config.yaml` 里 `dry_run: false`，然后 `docker compose up -d` 重启。

<details>
<summary>不用 Docker（systemd）</summary>

```bash
apt install -y python3-venv
useradd -r -s /usr/sbin/nologin tgtrader
cd /opt/tgtrader && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m tgtrader.main --login
chown -R tgtrader /opt/tgtrader
cp deploy/tgtrader.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now tgtrader
journalctl -u tgtrader -f
```
</details>

## 在 Telegram 里控制

把指令发到你自己的「收藏夹 / Saved Messages」：

| 指令 | 作用 |
|---|---|
| `/status` | 模式、账户权益、当前持仓、**各频道战绩** |
| `/pause` / `/resume` | 暂停 / 恢复跟单（已有仓位的止损止盈照常） |
| `/closeall` | 立即平掉机器人开的所有仓位并暂停 |
| `/recent` | 最近 10 笔记录 |
| `/help` | 帮助 |

## 主要配置（`config.yaml`）

| 项 | 默认 | 说明 |
|---|---|---|
| `dry_run` | `true` | 模拟盘 |
| `risk.sizing` | `fixed_margin` | `fixed_margin` 每单固定保证金；`fixed_risk` 每单打止损固定亏多少 U |
| `risk.margin_usdt` | 10 | 每单保证金 |
| `risk.max_leverage` | 10 | 杠杆上限 |
| `risk.require_stop_loss` | `true` | 没有止损的信号不跟 |
| `risk.max_open_positions` | 3 | 同时最多持仓 |
| `risk.daily_loss_limit_pct` | 10 | 当日亏损达 10% 停止开新仓 |
| `risk.max_entry_deviation_pct` | 1.5 | 价格偏离进场区超过 1.5% 不追，改挂限价 |
| `risk.follow_updates` | `true` | 跟随频道的移止损 / 减仓 / 平仓指令 |
| `channels[].margin_usdt` 等 | — | 每个频道可单独设置仓位和杠杆 |
| `llm.enabled` | `false` | 用 Claude 解析规则解析不了的消息 |

## 开发

```bash
pip install -r requirements-dev.txt
pytest
```

代码结构：

```
tgtrader/
  main.py        Telegram 监听、指令、主循环
  parser.py      信号解析（规则）
  llm_parser.py  可选的 Claude 解析
  risk.py        风控和下单计划
  trader.py      ccxt 下单、止盈止损、同步、模拟盘
  store.py       SQLite 记录（data/tgtrader.db）
  config.py      配置加载
```

**免责声明**：合约交易风险极高，可能损失全部本金。本项目仅为工具，不构成投资建议。
