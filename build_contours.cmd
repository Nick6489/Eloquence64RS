@echo off
setlocal
cd /d "%~dp0"
rem Pinned independent module release; never build an unreviewed moving branch.
set "contour_revision=24b9cb121aa3c4f55a6aaacd640f1fe8f03e020a"
if not exist "build\contours\.git" (
    git clone --no-checkout https://github.com/Nick6489/eloquence-contours.git build\contours
    if errorlevel 1 exit /b 1
)
git -C build\contours diff --quiet HEAD --
if errorlevel 1 (
    echo Contour checkout has local edits. Preserve them before rebuilding.
    exit /b 1
)
git -C build\contours cat-file -e %contour_revision%
if errorlevel 1 (
    git -C build\contours fetch origin %contour_revision%
    if errorlevel 1 exit /b 1
)
git -C build\contours checkout --detach %contour_revision%
if errorlevel 1 exit /b 1
set "RUSTFLAGS=-C target-feature=+crt-static"
cargo build --locked --release --target x86_64-pc-windows-msvc --manifest-path build\contours\Cargo.toml
if errorlevel 1 exit /b 1
cargo build --locked --release --target i686-pc-windows-msvc --manifest-path build\contours\Cargo.toml
if errorlevel 1 exit /b 1
copy /Y "build\contours\target\x86_64-pc-windows-msvc\release\eloquence_contours.dll" "addon\synthDrivers\eloquence_contours.dll"
if errorlevel 1 exit /b 1
copy /Y "build\contours\target\i686-pc-windows-msvc\release\eloquence_contours.dll" "addon\synthDrivers\eloquence_contours32.dll"
if errorlevel 1 exit /b 1
echo Built addon\synthDrivers\eloquence_contours.dll at %contour_revision%
copy /Y "build\contours\python\eloquence_contours.py" "addon\synthDrivers\_eloquence_contours.py"
if errorlevel 1 exit /b 1
copy /Y "build\contours\LICENSE" "addon\synthDrivers\contours-COPYING.txt"
if errorlevel 1 exit /b 1
