#!/bin/bash
# macOS: double-click this file to run webcrawler.
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "没有找到 Python 3。请先安装: https://www.python.org/downloads/"
  read -r -p "按回车退出..." _
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "首次运行, 正在安装依赖 (只需要一次)..."
  python3 -m venv .venv || { read -r -p "创建虚拟环境失败, 按回车退出..." _; exit 1; }
  .venv/bin/python -m pip install --upgrade pip -q
  .venv/bin/python -m pip install -q -r requirements.txt || { read -r -p "安装依赖失败, 按回车退出..." _; exit 1; }
fi

.venv/bin/python -m webcrawler "$@"
echo
read -r -p "完成。按回车关闭窗口..." _
