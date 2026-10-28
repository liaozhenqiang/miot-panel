# Windows camera binaries

## go2rtc

`go2rtc_win64_hidden.exe`, `go2rtc_win32_hidden.exe` and `go2rtc_win_arm64_hidden.exe` are built from [AlexxIT/go2rtc v1.9.15](https://github.com/AlexxIT/go2rtc/tree/v1.9.15) (MIT, see `LICENSE-go2rtc.txt`).

Only change: `pkg/shell/procattr_windows.go` sets `syscall.SysProcAttr{HideWindow: true}` for child processes, so ffmpeg transcode processes do not open a console window on Windows. No streaming logic is modified.

Build commands:

```sh
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -ldflags "-s -w" -trimpath -o go2rtc_win64_hidden.exe
GOOS=windows GOARCH=386   CGO_ENABLED=0 go build -ldflags "-s -w" -trimpath -o go2rtc_win32_hidden.exe
GOOS=windows GOARCH=arm64 CGO_ENABLED=0 go build -ldflags "-s -w" -trimpath -o go2rtc_win_arm64_hidden.exe
```

SHA256:

```
d1331144e1fc7520be8422aab8b11b8bef73517b2931e4107d9d677636ad1bd9  go2rtc_win64_hidden.exe
61d90f76e707e6cc52409a9293ab51d2dc1605e909914e15567c1022091e33ab  go2rtc_win32_hidden.exe
3e507bc54951dfbf6ec4989d2fe7442c510a90f3c88b004bb41789b02216d8e5  go2rtc_win_arm64_hidden.exe
```

## ffmpeg (Windows ARM64)

`ffmpeg_win_arm64.exe.gz` is extracted from the BtbN `ffmpeg-master-latest-winarm64-gpl.zip` static build (GPL, see `LICENSE-ffmpeg.txt`). It is only used when the panel runs as native ARM64 Python on Windows; x64 emulation uses the regular x64 ffmpeg build.

```
3a8e1e6f473fe96149192d2d49138e1d894f74eb05bb9cc830c27abdf916a099  ffmpeg_win_arm64.exe.gz
```
