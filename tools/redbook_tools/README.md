# redbook-tools

旧采编工作流的独立 E 盘工具包快照，提供 `python -m redbook_tools` CLI 和 `src`/`apps` 领域模块。它的运行数据由 `REDBOOK_RUNTIME_ROOT` 指向 `E:\AI\codex\redbook_runtime`；不依赖旧 `redbook_workflow` 目录。

安装及测试请见 `E:\AI\codex\redbook_agent\README.md`。旧 CLI 仍保留原实现；本快照与旧源代码尚未建立自动同步，因此跨入口修复要做双侧回归。不要把 `.env.gui`、API Key、浏览器 profile 或运行数据提交到 Git。
