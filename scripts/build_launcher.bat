@echo off
rem 重新编译 Noesia.exe（只改图标或改脚本时需要跑一次）。
rem 用的是系统自带的 .NET Framework 编译器，不用装任何东西。
cd /d "%~dp0.."
"%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /nologo /target:winexe /platform:anycpu ^
  /win32icon:"assets\noesia.ico" /out:"Noesia.exe" ^
  /r:System.dll /r:System.Windows.Forms.dll "scripts\NoesiaLauncher.cs"
if errorlevel 1 pause
