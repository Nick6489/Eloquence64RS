@echo off
setlocal
set "here=%~dp0"
cd /d "%here%"

set "RUSTFLAGS=-C target-feature=+crt-static"
cargo build --release --target i686-pc-windows-msvc --manifest-path native_host\Cargo.toml
if errorlevel 1 exit /b %errorlevel%

copy /Y "native_host\target\i686-pc-windows-msvc\release\eloquence_host32.exe" "addon\synthDrivers\eloquence_host32.exe"
if errorlevel 1 exit /b %errorlevel%

echo Built addon\synthDrivers\eloquence_host32.exe
