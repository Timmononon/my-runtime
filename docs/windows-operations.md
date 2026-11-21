# Windows 桌面版

## 安装和首次使用

1. 从 [Releases](https://github.com/Timmononon/my-runtime/releases) 下载适用于 Windows x64 的 `Aster-Setup-0.1.21-win-x64.exe`，双击后按向导为当前用户安装。
2. 从开始菜单打开 **Aster**。首次打开会自动登录本机工作台。点击首页「配置模型密钥」，选择 DeepSeek 或自己的兼容提供商，输入 API Key 并保存。
3. 关闭窗口可以让后台继续工作。若要同时停服，选择菜单「Aster → 退出并停止后台」。

默认安装位置是 `%LOCALAPPDATA%\Programs\Aster`，个人配置和对话默认保存在 `%USERPROFILE%\.aster`。再次运行安装包可以覆盖安装。通过 Windows「设置 → 应用 → 已安装的应用」卸载后，个人数据仍可供下次安装使用。

## 可选工具

网页自动化使用单独安装的 Chromium。飞书 CLI 安装脚本需要 Node.js 与 npm，账户授权在用户自己的电脑上完成。以下命令使用默认安装位置。[飞书配置和授权](feishu-cli.md)

```powershell
$resources = Join-Path $env:LOCALAPPDATA 'Programs\Aster\resources'
# 配置网页自动化时使用
& "$resources\runtime\python.exe" -m playwright install chromium
# 配置飞书时使用
& "$resources\tools\Install-FeishuCLI.ps1"
```

## 本机桌面版

源码开发环境使用 Python 3.12 x64 和 Node.js 22.12 或更高版本。在项目根目录安装 Python 环境后运行 `.\scripts\Install-AsterDesktop.ps1` 可以创建开发快捷方式。脚本支持 `-DataDir`、`-PythonExe`、`-Cwd` 和 `-Port`。桌面测试命令为 `npm.cmd --prefix desktop test`。

## 构建可分发的安装包

在 64 位 Windows 开发机上使用 64 位 Python 3.12 和 Node.js 22.12 或更高版本。从仓库根目录执行以下命令。示例在用户临时目录中新建一个随机短目录，为 Python 编译深层模块时生成的文件名留出空间。若 `%TEMP%` 或仓库路径很深，可将 `$stageRoot` 改为已有且可写的短目录，例如 `D:\tmp`。若 `%TEMP%` 本身很深，也请在当前 PowerShell 窗口中将 `$env:TEMP` 和 `$env:TMP` 设为同一短目录。

```powershell
$ErrorActionPreference = 'Stop'
$stageRoot = [IO.Path]::GetTempPath()
$stageName = [IO.Path]::GetFileNameWithoutExtension([IO.Path]::GetRandomFileName())
$stage = (New-Item -ItemType Directory -Path (Join-Path $stageRoot $stageName) -ErrorAction Stop).FullName
$python = (Resolve-Path '.venv\Scripts\python.exe').Path
& $python -m venv "$stage\build-venv"
if ($LASTEXITCODE) { throw '创建构建环境失败' }
$builder = "$stage\build-venv\Scripts\python.exe"
& $builder -m pip install -r build-requirements.lock
if ($LASTEXITCODE) { throw '安装 wheel 构建依赖失败' }
npm.cmd --prefix frontend ci
if ($LASTEXITCODE) { throw '安装前端依赖失败' }
npm.cmd --prefix desktop ci
if ($LASTEXITCODE) { throw '安装 Electron 构建依赖失败' }
npm.cmd --prefix frontend run build -- --outDir "$stage\renderer"
if ($LASTEXITCODE) { throw '构建工作台失败' }
& $python scripts/build_delivery.py --build-python $builder --static-root "$stage\renderer" --output "$stage\wheel"
if ($LASTEXITCODE) { throw '构建 Aster wheel 失败' }
$wheel = (Get-ChildItem "$stage\wheel\aster_runtime-*.whl" -File).FullName
& $python scripts/build_desktop_runtime.py --wheel $wheel --output "$stage\runtime"
if ($LASTEXITCODE) { throw '构建独立 Python 运行时失败' }
& $python scripts/package_desktop.py --runtime "$stage\runtime" --output dist
if ($LASTEXITCODE) { throw '构建安装包失败' }
```

将 `dist` 中生成的 `Aster-Setup-<版本>-win-x64.exe` 发给使用者即可。构建机首次运行会下载指定版本的解释器、依赖与安装器工具。安装向导和收件人的首次启动均从本地文件运行。安装程序默认位于 `%LOCALAPPDATA%\Programs\Aster`，运行数据默认位于 `%USERPROFILE%\.aster`。
