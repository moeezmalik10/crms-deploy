# tools/

Put `nssm.exe` here (download from https://nssm.cc/download, the `win64` build for a 64-bit
PC) if `install_service.bat` can't find it on your `PATH`. `install_service.ps1` looks for it
at `tools\nssm.exe` relative to the agent folder before giving up.

`nssm.exe` itself is not committed to this repo - download it yourself.
