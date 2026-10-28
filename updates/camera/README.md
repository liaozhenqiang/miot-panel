# Windows go2rtc build

`go2rtc_win64_hidden.exe` is built from [AlexxIT/go2rtc v1.9.15](https://github.com/AlexxIT/go2rtc/tree/v1.9.15) (MIT, see `LICENSE-go2rtc.txt`).

Only change: `pkg/shell/procattr_windows.go` sets `syscall.SysProcAttr{HideWindow: true}` for child processes, so ffmpeg transcode processes do not open a console window on Windows. No streaming logic is modified.

Build command:

```sh
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -ldflags "-s -w" -trimpath -o go2rtc_win64_hidden.exe
```

SHA256:

```
d1331144e1fc7520be8422aab8b11b8bef73517b2931e4107d9d677636ad1bd9
```
