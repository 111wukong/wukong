# 打包成桌面应用

把 wukong 打成一个**自包含**的应用：macOS 上是 `.app` + `.dmg`，Windows 上是单个 `.exe`。用户不需要装 Python、不需要 `pip install`。

## 为什么分两个平台构建

PyInstaller **不能跨平台编译**。它把当前解释器和当前平台的二进制一起收进产物，所以：

- 在 macOS 上只能产出 `.app` / `.dmg`；
- 在 Windows 上只能产出 `.exe`。

因此 `packaging/build.py` 在哪个平台跑，就产出哪个平台的包。`release.yml` 用 `macos-latest` 和 `windows-latest` 两个 runner 各建各的。

## 本地构建

```bash
pip install -e ".[all]" pyinstaller
python packaging/build.py
```

产物：

| 平台 | 产物 |
|---|---|
| macOS | `dist/wukong.app`、`dist/wukong-<version>.dmg` |
| Windows | `dist/wukong.exe` |

`--sign` 会在封装 `.dmg` 之前对 `.app` 做 **ad-hoc 签名**（`codesign --sign -`），这样下载后 Gatekeeper 不会直接杀掉它。真正的发布需要 Developer ID 证书 + notarization。

## 打进包里的是什么

- `src/wukong/api/console/**` —— Web 控制台的静态资源。`api/app.py` 用 `Path(__file__).parent / "console"` 定位它，所以 spec 把这份目录放到包内的 `wukong/api/console`，路径才对得上。
- `uvicorn` 的子模块 —— 它按名字动态 import loop/protocol，静态分析看不到，spec 用 `collect_submodules` 显式收。
- `webview`（pywebview）—— 在 `launcher._webview()` 里**懒加载**，静态分析同样看不到，用 `collect_all` 收。
- `sqlite_vec` —— 可选的原生加速库；装了就被 `collect_all` 收进去，没装则跳过（纯 Python 路径等价，只是慢）。

**不打进去**的：`skills/`、`workflows/`、`examples/`。技能是用户数据，运行时从 `~/.wukong/skills` 之类的 home 目录读取；把示例塞进只读的包内目录，用户既不能改也不能晋升。

## 首次启动

双击应用时，工作目录可能是 `/`（macOS）或安装目录（Windows），两者都不该成为 workspace。`packaging/entry_desktop.py` 检测到 cwd 不可写时回退到用户 home，再交给 `wukong.desktop.__main__`。第一次启动会在 home 下建配置与数据库。

## 在本项目的 WorkBuddy 沙箱里构建

WorkBuddy 的文件代理会拦截工作区外的 `mkdir`，而 `pip` 的构建临时目录、`iconutil` 的 iconset 都默认落在 `/var/folders`。在沙箱里构建时把 `TMPDIR` 指到仓库内：

```bash
export TMPDIR="$PWD/.pytest-tmp/pip-tmp"
export PIP_CACHE_DIR="$PWD/.pytest-tmp/pip-cache"
python packaging/build.py
```

普通终端和 CI 不需要这一步。

## 校验产物

```bash
# macOS
dist/wukong.app/Contents/MacOS/wukong --check     # 起停服务器，不开窗口
# 或挂载 dmg
hdiutil attach dist/wukong-*.dmg
```

`--check` 走的是真实服务器生命周期（建 app、监听、退出），不需要显示器，所以无头环境也能验。

`--serve` 保持服务器运行、不开窗口，并打印控制台地址——没有显示器的机器用它，打包冒烟测试也用它：

```bash
dist/wukong.app/Contents/MacOS/wukong --serve --token t
# console: http://127.0.0.1:<port>/#t
curl -s -H 'X-WUKONG-Token: t' http://127.0.0.1:<port>/api/v1/health
```

## 已知边界

- **未签名/未公证**：macOS 首次打开会提示「无法验证开发者」，右键 → 打开即可；正式分发需要 Apple 开发者证书。Windows 的 SmartScreen 同理。
- **体积**：pywebview + pyobjc + uvicorn 一起收进来，`.app` 通常几十到一百多 MB。这是自包含的代价；要更小就改成 `pip install` 分发。
- **交叉编译**：不存在。别指望在 mac 上产出 Windows `.exe`。
