# 本地验收产物

`output/` 用于保存正式前端的本地隔离验收产物。试讲复盘和分析文档放在 `docs/`。

## `formal-local/`

这是隔离验收脚本的默认运行目录。脚本启动时会按需创建它，也可以用 `--runtime-dir` 指定其他目录。 [验收脚本](../scripts/run_formal.py)

运行后，这里可能包含课堂日志和报告、本次验收的 `settings.local.json`，以及记录模型提供方请求次数和用量的 `provider-requests.jsonl`。同一目录再次启动时会继续累计已有的请求次数。

已有的 `audit-20261018-severe-bugs/` 和 `recovery-fix-20261018/` 是两次历史验收的附件，分别记录正式前端的问题检查和修复后的验证。每组的 `report.md` 说明当时的范围和结果，同目录保留截图、日志或补丁。

`formal-local/` 已被 Git 忽略。里面的验收附件仅保存在本机，删除后无法通过 Git 找回。 [忽略规则](../.gitignore)
