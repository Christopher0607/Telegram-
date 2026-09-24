#!/usr/bin/env bash
# 一键安装 / 更新 Telegram 信号跟单机器人（Ubuntu / Debian）
#
# 在服务器上执行（DigitalOcean 网页控制台里粘贴即可）：
#   curl -fsSL https://raw.githubusercontent.com/Christopher0607/Telegram-/claude/telegram-monitoring-trading-bot-vx8v9h/install.sh -o install.sh && bash install.sh
#
# 再次运行 = 更新代码并重启（保留 .env、config.yaml、data/）。
# 想重新填密钥：bash install.sh --reconfigure
set -euo pipefail

REPO="Christopher0607/Telegram-"
BRANCH="claude/telegram-monitoring-trading-bot-vx8v9h"
DIR="/opt/tg-signal-trader"
RECONF=0
[ "${1:-}" = "--reconfigure" ] && RECONF=1

say()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "请用 root 运行：sudo bash install.sh"; exit 1; }

# ---------------------------------------------------------------- 1. Docker
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  say "安装 Docker（约 1-2 分钟）"
  apt-get update -qq
  apt-get install -y -qq curl ca-certificates >/dev/null
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null 2>&1 || true

# ---------------------------------------------------------------- 2. 代码
say "下载最新代码到 $DIR"
TMP="$(mktemp -d)"
curl -fsSL "https://codeload.github.com/${REPO}/tar.gz/refs/heads/${BRANCH}" | tar -xz -C "$TMP" --strip-components=1
mkdir -p "$DIR/data"
# 代码文件总是更新；.env / config.yaml / data 保留你已有的
for f in "$TMP"/* "$TMP"/.[!.]*; do
  [ -e "$f" ] || continue
  name="$(basename "$f")"
  case "$name" in
    .env|data) continue ;;
    config.yaml) [ -f "$DIR/config.yaml" ] && continue ;;
  esac
  cp -a "$f" "$DIR/"
done
rm -rf "$TMP"
cd "$DIR"

# ---------------------------------------------------------------- 3. 密钥
ask() {  # ask 变量名 "提示" [secret] [optional]
  local var="$1" prompt="$2" secret="${3:-}" optional="${4:-}" val=""
  while :; do
    if [ "$secret" = "secret" ]; then
      read -r -s -p "$prompt: " val; echo
    else
      read -r -p "$prompt: " val
    fi
    val="$(printf '%s' "$val" | tr -d '[:space:]')"
    [ -n "$val" ] || [ "$optional" = "optional" ] && break
    warn "不能为空，请重新输入"
  done
  printf -v "$var" '%s' "$val"
}

if [ ! -f .env ] || [ "$RECONF" -eq 1 ]; then
  say "填写密钥（输入的密钥不会显示在屏幕上，直接粘贴后回车即可）"
  echo "1) Telegram API：https://my.telegram.org → API development tools"
  ask TG_API_ID   "TG_API_ID（纯数字）"
  ask TG_API_HASH "TG_API_HASH" secret
  ask TG_PHONE    "监听账号手机号（带国家码，如 +8613800000000）"
  echo
  echo "2) 通知机器人：在 Telegram 找 @BotFather 发 /newbot 建一个新机器人，并先给它发一次 /start"
  ask TG_BOT_TOKEN "TG_BOT_TOKEN" secret
  ask TG_OWNER_ID  "TG_OWNER_ID（监听用小号时填你大号的数字 id，否则直接回车）" "" optional
  echo
  echo "3) AI 解析"
  ask DEEPSEEK_API_KEY "DEEPSEEK_API_KEY" secret
  echo
  echo "4) Bitget API（只跑模拟盘可以直接回车跳过，以后用 bash install.sh --reconfigure 再填）"
  ask BITGET_API_KEY        "BITGET_API_KEY" secret optional
  ask BITGET_API_SECRET     "BITGET_API_SECRET" secret optional
  ask BITGET_API_PASSPHRASE "BITGET_API_PASSPHRASE" secret optional

  umask 077
  cat > .env <<EOF
TG_API_ID=${TG_API_ID}
TG_API_HASH=${TG_API_HASH}
TG_PHONE=${TG_PHONE}
TG_BOT_TOKEN=${TG_BOT_TOKEN}
TG_OWNER_ID=${TG_OWNER_ID}
DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY}
ANTHROPIC_API_KEY=
BITGET_API_KEY=${BITGET_API_KEY}
BITGET_API_SECRET=${BITGET_API_SECRET}
BITGET_API_PASSPHRASE=${BITGET_API_PASSPHRASE}
EOF
  chmod 600 .env
  umask 022
  echo "✅ 已保存到 $DIR/.env（只有 root 可读）"
fi

# ---------------------------------------------------------------- 4. 构建 + 自检
say "构建镜像"
docker compose build -q
say "离线自检"
docker compose run --rm bot python selftest.py | tail -1

# ---------------------------------------------------------------- 5. Telegram 登录
if [ ! -f data/telegram.session ]; then
  say "登录监听账号：Telegram 会给这个号发验证码，填进来（有二步验证还要输密码）"
  docker compose run --rm bot python main.py login
fi

# ---------------------------------------------------------------- 6. 检查 + 启动
say "检查配置（交易所 / AI / 频道 / 通知机器人）"
docker compose run --rm bot python main.py check || true

say "启动机器人"
docker compose up -d
sleep 5
docker compose ps
echo
echo "✅ 完成！你的 Telegram 机器人会收到「🚀 信号跟单已启动」。默认全部是模拟盘，不会动你的钱。"
echo "   看日志：   cd $DIR && docker compose logs -f --tail 50"
echo "   测试识别： cd $DIR && docker compose run --rm bot python main.py replay 30"
echo "   更新代码： bash install.sh"
