# Windows 测试与产物目录

## 前端发布检查

在 `web/` 运行 `bun run lint` 和 `bun run build`。构建会执行 TypeScript 检查、Vite 打包及 `scripts/verify-build.mjs`；该脚本通过构建清单核对 HTML 入口、动态页面和资源依赖，并确认实际 Dashboard 文件包含回复缓存卡片及 API。也可单独运行 `bun run verify-build` 检查现有产物。

打包前必须使用当前成功构建的完整 `src/aistudio_api/static/index.html` 和 `assets/`，不能把新增的 Vue 源码与旧静态资源混装。`.vite/manifest.json` 用于构建验证，可作为发布元数据保存。对解包后的生产资源执行浏览器验证，确认「控制面板」中的「回复缓存」及保留轮数操作；仅有压缩包校验成功不代表包含新功能。

## Python 测试

在项目目录执行：

```powershell
& .\scripts\test.ps1
& .\scripts\test.ps1 -PytestArgs @('tests/unit/test_request_analytics.py', '-q')
```

脚本使用 `uv` 和项目现有 `.venv\Scripts\python.exe`，不安装依赖、不下载解释器或重建环境。需要已有的 Python 3.11 以上环境及项目测试依赖；可通过 `-PythonExe` 指定其他准备好的解释器。

默认产物根从项目所在工作区推导为同级 `<工作区名>-tests`。当前每次输出到 `D:\gpt-tests\aistudio-api\runs\<时间戳-GUID>\`：

- `tmp/`：pytest 的临时数据库和隔离配置。
- `cache/`：本次 pytest 缓存。
- `junit.xml`：机器可读测试报告。
- `system-tmp/`：本次进程及子进程的通用临时文件。
- `uv-cache/`：本次 uv 缓存。

可用 `-ArtifactsRoot` 覆盖产物根，也可设置 `GPT_TESTS_ROOT`；目录必须位于工作区外。每次使用独立目录，不覆盖之前的结果。脚本禁用新增 Python 字节码，并在结束时恢复修改过的环境变量和工作目录，返回 pytest 退出码。

项目 `pyproject.toml` 中的 addopts（包括 `-p no:anyio`）保持生效。脚本临时清空 `PYTEST_ADDOPTS`；额外选项通过 `-PytestArgs` 传入，不能覆盖受管的输出路径或配置文件。

2026-11-21 之前留在项目和工作区根目录的 pytest 临时产物已归档到 `D:\gpt-tests\archive\2026-11-21\`。测试源码和夹具仍在 `tests/`。

发布包位于 `D:\gpt-releases\aistudio-api\`。现有 `tmp/package_dashboard_release.py`、`tmp/audit_keepalive_archives.py` 已使用该默认目录，并支持 `GPT_RELEASES_ROOT`；`tmp/check_upgrade_stub.py` 的模拟临时文件也写到外部测试根。这些辅助脚本原本位于被 Git 忽略的 `tmp/`，仍是本机辅助文件。
