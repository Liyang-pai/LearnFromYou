#!/bin/zsh
PROJECT_ROOT="${0:A:h}"
cd "$PROJECT_ROOT" || exit 1
export PYTHONUTF8=1

finish() {
    local result="$1"
    if [[ "$result" != 0 && "$result" != 130 ]]; then
        echo "启动未完成，请查看上方错误，处理后重新双击启动。"
        if [[ -t 0 ]]; then
            read -r "?按回车键关闭窗口……"
        fi
    fi
    exit "$result"
}

echo "正在检测 Python（兼容 3.11–3.13，推荐 3.13）……"
PYTHON_EXECUTABLE=""
if [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
    PYTHON_EXECUTABLE=$("$PROJECT_ROOT/.venv/bin/python" -I -X utf8 "$PROJECT_ROOT/scripts/bootstrap.py" \
        --probe --venv "$PROJECT_ROOT/.venv" 2>/dev/null)
fi

if [[ -z "$PYTHON_EXECUTABLE" ]]; then
    for candidate in python3.13 python3.12 python3.11 python3 \
        /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 \
        /usr/local/bin/python3.13 /usr/local/bin/python3.12 /usr/local/bin/python3.11; do
        resolved=$(command -v "$candidate" 2>/dev/null) || continue
        # Apple's stub may launch the developer-tools installer instead of Python.
        if [[ "$resolved" == /usr/bin/python3 ]] && ! /usr/bin/xcode-select -p >/dev/null 2>&1; then
            continue
        fi
        PYTHON_EXECUTABLE=$("$resolved" -I -X utf8 "$PROJECT_ROOT/scripts/bootstrap.py" --probe 2>/dev/null)
        [[ -n "$PYTHON_EXECUTABLE" ]] && break
    done
fi

if [[ -z "$PYTHON_EXECUTABLE" ]]; then
    echo "未找到可用的标准版 Python 3.11–3.13。"
    echo "请从 https://www.python.org/downloads/release/python-31316/ 选择 macOS installer 安装，然后重新双击启动。"
    finish 1
fi

echo "使用 Python：$PYTHON_EXECUTABLE"
"$PYTHON_EXECUTABLE" -X utf8 "$PROJECT_ROOT/scripts/bootstrap.py" "$@"
finish "$?"

