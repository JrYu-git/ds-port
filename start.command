#!/bin/bash
# macOS / Linux 一键启动：双击或在终端里 ./start.command
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo ""
  echo "  [x] 没有找到 python3"
  echo ""
  echo "  请先安装 Python 3.9 或更高版本："
  echo "  https://www.python.org/downloads/"
  echo ""
  read -n 1 -s -r -p "按任意键退出..."
  exit 1
fi

echo ""
echo "  ds-port 正在启动，浏览器会自动打开"
echo "  用完按 Ctrl+C 停止"
echo ""

exec python3 server.py
