@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo === 抖音礼物采集 - Windows 打包 ===
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.10+
    pause
    exit /b 1
)

echo [1/3] 安装依赖...
python -m pip install -r requirements.txt pyinstaller -q
if errorlevel 1 (
    echo [错误] 依赖安装失败
    pause
    exit /b 1
)

if not exist config.json (
    echo [提示] 未找到 config.json，从 config.example.json 复制
    copy /Y config.example.json config.json >nul
)

echo [2/3] 开始打包 exe...
python -m PyInstaller collect_gifts.spec --noconfirm --clean
if errorlevel 1 (
    echo [错误] 打包失败
    pause
    exit /b 1
)

echo [3/3] 完成
echo.
echo 输出文件: dist\抖音礼物采集.exe
echo.
echo 使用说明:
echo   1. 将 dist\抖音礼物采集.exe 复制到任意文件夹
echo   2. 首次运行会在同目录生成 config.json（可编辑 sid_guard、backend_push）
echo   3. 双击 exe，输入房间 ID，点「开始采集」
echo   4. 礼物记录保存在同目录 gifts_房间ID.txt
echo   5. 推送到后台：编辑 config.json 的 backend_push（ws_url 或 http_url）
echo.
pause
