@echo off
rem ===================================================================
rem  启动顾问浮层（双击这个文件就行）
rem ===================================================================
rem  为什么用 pythonw：不带控制台窗口，双击后只有顾问窗，没有黑框跟着。
rem  为什么用系统 Python：项目里那个被精简过，没有 tkinter（界面库）。
rem
rem  下面两条都是踩过坑才写死的，别改：
rem
rem   1) 不用 start。
rem      商店版 Python 在 WindowsApps 下是个"应用执行别名"（0 字节的
rem      跳转文件）。经 start 启动时进程会直接消失 —— 用户端看到的就是
rem      "双击后黑窗一闪，什么都没发生"。而 pythonw 是 GUI 程序，
rem      命令行调它本来就立即返回、不会占住这个窗口，所以直接调就行。
rem
rem   2) 路径必须写全（用 %~dp0 打头）。
rem      别名启动时不保证继承当前目录，用相对路径会找不到 overlay.py。
rem
rem  pythonw 是个"哑"进程：崩了也不会说话。所以让 overlay.py 自己留
rem  文件，这里靠文件判断结果：
rem      overlay_started.txt   它起来了
rem      overlay_error.txt     它崩了，里面是错误堆栈
rem  失败时交给 Python 弹一个 Unicode 消息框
rem  （bat 的 echo 显示中文会乱码，不能用来报错）。
rem ===================================================================

setlocal
cd /d "%~dp0"
set "DIR=%~dp0"

set "PYW=%LOCALAPPDATA%\Microsoft\WindowsApps\pythonw.exe"
set "PY=%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe"
if not exist "%PYW%" set "PYW=pythonw"
if not exist "%PY%"  set "PY=python"

del "%DIR%overlay_started.txt" >nul 2>nul
del "%DIR%overlay_error.txt"   >nul 2>nul

"%PYW%" "%DIR%overlay.py"

rem 等它 3 秒（ping 是这里最通用的"睡一会儿"）
ping -n 4 127.0.0.1 >nul

if exist "%DIR%overlay_started.txt" exit /b 0

rem ---- 没起来：把原因弹到用户面前 ----
"%PY%" "%DIR%overlay.py" --show-error
exit /b 1
