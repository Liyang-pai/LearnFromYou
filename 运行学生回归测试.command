#!/bin/bash
# 在用户终端运行真实API学生回归及三次长课，串行执行并保存独立日志。
set -uo pipefail
cd -- "$(dirname -- "$0")" || exit 1

if [[ ! -x .venv/bin/python ]]; then
    echo "未找到项目 .venv/bin/python，请先完成项目环境安装。"
    read -r -p "按回车键关闭窗口……" lfy_test_ack
    exit 1
fi

mkdir -p logs || exit 1
lfy_test_output="logs/classroom-acceptance-$(date +%Y%m%d-%H%M%S)-$$"
echo "即将使用真实 API，执行完整学生反应回归和三次长课测试。"
echo "本轮串行运行，每次一个课堂；请保持终端打开。"
echo "证据目录：$PWD/$lfy_test_output"
echo "终端记录：$PWD/$lfy_test_output.console.log"

.venv/bin/python -u tests/live_classroom_acceptance.py \
    --live --concurrency 1 --output "$lfy_test_output" \
    2>&1 | tee "$lfy_test_output.console.log"
lfy_test_status=$?

if [[ "$lfy_test_status" -eq 0 ]]; then
    echo "测试执行完成。教学内容质量仍需结合逐轮记录和报告复核。"
else
    echo "测试未完成或发现异常，退出码：$lfy_test_status。请保留上述证据目录。"
fi
read -r -p "按回车键关闭窗口……" lfy_test_ack
exit "$lfy_test_status"
