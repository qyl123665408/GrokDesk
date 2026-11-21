# GrokDesk

Windows 桌面 AI 聊天工具。1.4.0 使用独立的 WebView2 窗口，支持聊天、附件、历史记录和本机电脑控制。关闭窗口会退出软件；重复启动会恢复已有窗口。

软件通过 OpenAI 兼容的 `/v1/models` 和 `/v1/chat/completions` 接口连接服务商。请使用自己的 API 地址、密钥和有权限的模型。本项目不包含 Grok 订阅、API 密钥或模型额度。

## 功能

- 流式聊天、停止生成、重新生成、多会话历史和搜索。
- 图片及常用文档附件、拖放文件、粘贴图片。
- Markdown 和 JSON 导出、深浅主题、输入草稿。
- 看屏幕、鼠标点击、文本输入、快捷键和滚动。
- 电脑控制只提供两种模式：**需要确认**（逐步批准）和 **完全权限**（任务内自动执行）。两种模式均可随时停止，使用当前 Windows 账户的权限。

电脑控制需要支持图片输入和动作输出的模型。屏幕截图会发送给配置的 API 服务商；截图不会自动保存为聊天附件。

## 直接运行桌面版

获得 `GrokDesk-Windows.zip` 后，将整个压缩包解压到可写目录，双击 `GrokDesk.exe`。请保留 `_internal` 等随附文件夹。

桌面窗口需要 Microsoft Edge WebView2 Runtime 和 .NET Framework 4.8。首次使用时，在「设置」填写 API 地址和密钥、获取模型并保存。详细操作、附件限制和常见问题见 [使用说明](使用说明.md)。

## 从源码运行

开发环境：Windows x64、Python 3.11、WebView2 Runtime、.NET Framework 4.8。下面命令在项目根目录的 PowerShell 中执行：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe app.py
```

`requirements.lock.txt` 固定运行和打包依赖版本；`requirements.txt` 列出直接依赖。前端资源位于 `web/vendor`，软件启动时无需从 CDN 下载界面脚本。

需要手动使用系统浏览器排障时，可运行 `app.py --browser`。仅运行本地服务可使用 `app.py --no-window --port 8765`。正常使用直接运行 `app.py` 即可。

## 测试

单元测试使用隔离数据和本地合成服务，不需要真实 API 密钥：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v
```

前端界面测试还需要 Node.js 20 或以上版本，以及 Playwright 的 Chromium：

```powershell
npm install
npm run install:browsers
.\.venv\Scripts\python.exe tests\run_ui_checks.py
```

默认使用 PATH 中的 `node` 和 Playwright 下载的 Chromium。可通过 `NODE_EXECUTABLE`、`PLAYWRIGHT_MODULE`、`PLAYWRIGHT_BROWSER_EXECUTABLE` 指定本机工具路径。

打包后，可以用独立的本地合成数据检查发布程序：

```powershell
.\.venv\Scripts\python.exe tests\verify_release.py
.\.venv\Scripts\python.exe tests\verify_control_release.py
.\.venv\Scripts\python.exe tests\verify_native_desktop_release.py
```

原生窗口测试需要可交互的 Windows 桌面。它会打开测试窗口、恢复最小化窗口并检查退出，不使用真实模型或真实历史。`tests\verify_native_ui.py` 提供额外的原生 WebView2 界面检查。

## 打包

先安装 Python 依赖，再执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
```

也可以用 `-PythonExecutable` 指定 Python 入口。构建输出为 `dist\GrokDesk\GrokDesk.exe` 和 `dist\GrokDesk-Windows.zip`。发布时应提供完整 ZIP，避免只复制 EXE。

## 更新和发布

在本地项目目录获取仓库的新提交：

```powershell
git pull --ff-only
```

更新源码后，关闭正在运行的 GrokDesk，安装变更后的 Python 依赖，再运行 `build.ps1` 重新生成桌面版。用户数据目录独立于源码；构建脚本会先备份已有便携数据，生成干净 ZIP 后再恢复本机数据。

GitHub Actions 会在每次推送后执行 Windows 单元测试和打包，并提供可下载的 ZIP 构建产物。推送以 `v` 开头的版本标签时，会创建对应的 GitHub Release 并附上 ZIP。自动构建不会执行需要可交互桌面的原生 GUI 测试，也不会使用个人 API 密钥。

## 本地数据和版本管理

程序会继续使用已有的便携 `GrokDeskData`；没有已有便携数据时，默认使用 `%LOCALAPPDATA%\GrokDesk`。设置里可以查看实际数据目录。

密钥由 Windows DPAPI 加密，绑定当前 Windows 用户。聊天保存在 SQLite，附件保存在本地数据目录。关闭软件后复制完整数据目录即可备份。

Git 只管理源码、测试、文档和第三方声明。`.gitignore` 排除了聊天、密钥设置、附件、浏览器缓存、测试输出、虚拟环境和构建产物。发布版本时保留第三方许可证，相关文件位于 `third_party`。
