# eSIM Switchboard

`eSIM Switchboard` 是一个运行在 macOS 上的本地运维面板，用来管理一台通过 USB 连接的 Android 手机。  
它把 `ADB`、`短信收件箱同步`、`eSIM 状态采集`、`eSIM 激活切换` 和 `网页可视化控制台` 组合在一起，适合做多 eSIM 管理、短信查看和远程辅助切换。

从当前页面效果上看，它提供了这些核心区域：

- 服务状态：ADB 可用性、监听线程状态、设备连接数
- eSIM 状态：总 eSIM 数、当前激活 eSIM 数、eSIM 明细、切换按钮
- 收件箱短信：历史短信列表、按 eSIM 名称筛选、分页浏览、本地时间显示
- 切换回放：切换步骤、步骤截图、切换成功/失败结果

## 功能概览

- 启动时自动检查并创建 `db.sqlite`
- 启动时自动同步 `adb shell dumpsys isub`
- 启动时自动全量拉取 `content://sms/inbox`
- 后台线程持续监听短信相关 `logcat`
- 短信按 Android `_id` 去重，必要时回退到内容指纹
- 短信列表按浏览器当地时区显示完整时间 `YYYY-MM-DD HH:mm:ss`
- 支持按 eSIM 名称筛选短信
- 支持在页面中直接切换激活 eSIM
- 切换过程中实时回传步骤截图和状态
- 页面与全部业务 API 支持密码登录保护

## 适用场景

- 你有一台 Android 手机，里面装了多张 eSIM
- 你希望在 Mac 上查看短信，而不是一直盯着手机
- 你需要频繁切换不同 eSIM，并想保留切换过程截图
- 你需要一个轻量本地工具，而不是云服务或复杂后台

## 项目结构

```text
app/
  adb.py                 ADB 命令封装与解析
  config.py              环境配置
  db.py                  SQLite 初始化与查询
  main.py                FastAPI 入口
  models.py              数据模型
  monitor.py             短信监听线程
  services.py            eSIM / 短信同步服务
  switch_service.py      eSIM 切换与步骤截图服务
  templates/index.html   Web 控制台
assets/
  esim-switchboard-icon.svg
runtime/
  switch_screenshots/    eSIM 切换步骤截图
scripts/
  manage_launch_agent.sh macOS LaunchAgent 管理脚本
tests/
requirements.txt
pytest.ini
```

## 环境要求

- macOS
- Python 3.12
- 一台已启用开发者选项和 USB 调试的 Android 手机
- ADB 可执行文件
- 手机可通过 `uiautomator2` 驱动进行界面点击

## 安装部署

### 1. 创建虚拟环境

如果项目里还没有 `.venv`：

```bash
python3 -m venv .venv
```

### 2. 安装依赖

```bash
./.venv/bin/pip install -r requirements.txt
```

关键依赖包括：

- `fastapi`
- `uvicorn`
- `jinja2`
- `httpx`
- `pytest`
- `uiautomator2`
- `adbutils`
- `pillow`

### 3. 配置 ADB

如果 `adb` 已经在系统 `PATH` 里，可以直接验证：

```bash
adb version
```

如果终端提示 `command not found: adb`，请显式设置 `ADB_PATH`，例如：

```bash
export ADB_PATH="/Users/你的用户名/Library/Android/sdk/platform-tools/adb"
```

如果你未来会接多台设备，也可以指定设备序列号：

```bash
export ADB_DEVICE_SERIAL="你的设备序列号"
```

### 4. 配置访问密码

项目支持一个简单密码层，页面和 API 都会受保护。  
启动前建议设置：

```bash
export APP_PASSWORD="你的访问密码"
```

如果不设置 `APP_PASSWORD`，当前实现会把密码保护视为关闭。

### 5. 启动服务

推荐方式：

```bash
export ADB_PATH="/Users/你的用户名/Library/Android/sdk/platform-tools/adb"
export APP_PASSWORD="你的访问密码"
./.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

或者直接运行入口：

```bash
export ADB_PATH="/Users/你的用户名/Library/Android/sdk/platform-tools/adb"
export APP_PASSWORD="你的访问密码"
./.venv/bin/python app/main.py
```

### 6. 使用 LaunchAgent 在后台启动

如果你希望它在 macOS 登录后自动拉起，或者长期在后台运行，推荐直接用项目自带脚本：

```bash
scripts/manage_launch_agent.sh
```

最常见安装方式：

```bash
APP_PASSWORD="你的访问密码" \
ADB_PATH="/Users/你的用户名/Library/Android/sdk/platform-tools/adb" \
scripts/manage_launch_agent.sh install
```

脚本会：

- 生成 `~/Library/LaunchAgents/com.guohai.esim-switchboard.plist`
- 用当前用户的 `launchd` 域加载服务
- 自动执行 `uvicorn app.main:app --host 127.0.0.1 --port 8000`
- 设置为用户登录后自动拉起

如果系统 `PATH` 中已有 `adb`，或者 `~/Library/Android/sdk/platform-tools/adb` 存在，脚本也会自动尝试探测 `ADB_PATH`。

## 启动后会发生什么

服务启动后会自动执行：

1. 创建 `db.sqlite` 和表结构
2. 拉取 `adb shell dumpsys isub`
3. 写入 eSIM 快照和 eSIM 明细
4. 拉取 Android 收件箱短信
5. 将短信写入 SQLite
6. 启动短信监听线程
7. 准备 eSIM 切换截图目录 `runtime/switch_screenshots/`

## 页面与使用方式

### 打开页面

- 首页：[http://127.0.0.1:8000/](http://127.0.0.1:8000/)
- Swagger 文档：[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

### 登录

首次进入页面时会出现密码输入层。  
输入 `APP_PASSWORD` 对应的密码后，页面才会加载：

- 服务状态
- eSIM 状态
- 收件箱短信
- eSIM 切换实时进度

点击“退出登录”后会清除登录态，并重新回到密码层。

### 查看短信

在“收件箱短信”区域可以：

- 查看历史短信分页列表
- 按 eSIM 名称筛选
- 查看本地时间格式的短信时间
- 手动点击“拉取全部历史短信”重新全量同步

时间列现在显示的是浏览器本地时区时间，而不是原始毫秒时间戳。

### 查看 eSIM 状态

在“eSIM 状态”区域可以看到：

- 总 eSIM 数
- 当前激活 eSIM 数
- 最新同步时间
- 每张 eSIM 的 `sub_id`、名称、运营商、状态

### 切换 eSIM

在每一行 eSIM 后面可以直接点击“切换到此 eSIM”。

页面会先要求选择一个锁定时长：`10 / 20 / 30` 分钟。  
只有切换成功后才会进入锁定期；锁定期间无法再次发起新的 eSIM 切换。

切换过程中，页面会：

- 进入全屏蒙层
- 禁止底层一切点击操作
- 展示步骤时间线
- 展示每一步点击后的手机截图

当前切换流程大致包括：

1. 点亮屏幕
2. 解锁并收起锁屏层
3. 复位设置应用
4. 打开网络设置
5. 进入 SIM 卡列表
6. 选择目标 eSIM
7. 启用目标 eSIM
8. 确认切换
9. 等待切换生效并再次确认

切换完成后，服务会等待一段时间，再重新截图，并通过一次新的 `adb shell dumpsys isub` 结果确认激活是否真的成功。

## API 说明

### 认证接口

- `POST /api/auth/login`
- `POST /api/auth/logout`
- `GET /api/auth/status`

### eSIM 接口

- `GET /api/esim/latest`
- `POST /api/esim/sync`
- `POST /api/esim/switch`
- `GET /api/esim/switch/status`
- `GET /api/esim/switch/stream`

其中 `POST /api/esim/switch` 的请求体必须包含：

- `display_name`
- `lock_minutes`，可选值只允许 `10`、`20`、`30`

### 短信接口

- `GET /api/sms`
- `POST /api/sms/sync`
- `POST /api/sms/sync-all`

### 其他接口

- `GET /api/health`
- `GET /api/monitor/status`

## API 使用示例

### 登录

```bash
curl -i -X POST http://127.0.0.1:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"password":"你的访问密码"}'
```

### 查询健康状态

```bash
curl --cookie "esim_switch_auth=你的cookie值" \
  http://127.0.0.1:8000/api/health
```

### 拉取全部历史短信

```bash
curl --cookie "esim_switch_auth=你的cookie值" \
  -X POST http://127.0.0.1:8000/api/sms/sync-all
```

### 查询短信列表

```bash
curl --cookie "esim_switch_auth=你的cookie值" \
  "http://127.0.0.1:8000/api/sms?page=1&page_size=20"
```

### 按 eSIM 名称筛选短信

```bash
curl --cookie "esim_switch_auth=你的cookie值" \
  "http://127.0.0.1:8000/api/sms?page=1&page_size=20&display_name=Club+85264220597"
```

### 发起 eSIM 切换

```bash
curl --cookie "esim_switch_auth=你的cookie值" \
  -X POST http://127.0.0.1:8000/api/esim/switch \
  -H 'Content-Type: application/json' \
  -d '{"display_name":"Club+85264220597","lock_minutes":10}'
```

## 数据与产物

### SQLite 数据库

数据库默认位于项目根目录：

```text
db.sqlite
```

主要表包括：

- `esim_snapshots`
- `esim_subscriptions`
- `sms_messages`
- `app_state`

### 切换截图

eSIM 切换步骤截图默认保存在：

```text
runtime/switch_screenshots/
```

每次任务会生成一个单独目录，里面按步骤顺序保存 PNG 截图。

## 可配置环境变量

| 变量名 | 默认值 | 说明 |
| --- | --- | --- |
| `ADB_PATH` | `adb` | ADB 可执行文件路径 |
| `ADB_DEVICE_SERIAL` | 空 | 多设备场景下指定目标设备 |
| `DB_PATH` | `./db.sqlite` | SQLite 文件路径 |
| `APP_PASSWORD` | 空 | 页面/API 访问密码；为空则不启用密码保护 |
| `APP_AUTH_COOKIE_NAME` | `esim_switch_auth` | 登录态 Cookie 名 |
| `SMS_SYNC_DELAY_SECONDS` | `1.5` | 新短信日志命中后回拉短信的等待时间 |
| `ADB_RECONNECT_DELAY_SECONDS` | `5` | `logcat` 异常退出后的重连等待时间 |
| `DEFAULT_PAGE_SIZE` | `50` | `/api/sms` 默认分页大小 |
| `MAX_PAGE_SIZE` | `200` | `/api/sms` 最大分页大小 |
| `SWITCH_SCREENSHOT_DIR` | `./runtime/switch_screenshots` | eSIM 切换截图目录 |
| `SWITCH_STEP_DELAY_SECONDS` | `1` | 每一步点击后的等待时间 |
| `SWITCH_CONFIRM_WAIT_SECONDS` | `10` | 确认切换后等待并再次截图的时间 |

## 运行测试

```bash
./.venv/bin/pytest -q
```

当前覆盖范围包括：

- `dumpsys isub` 解析
- 短信查询结果解析
- 短信去重写库
- 登录鉴权逻辑
- eSIM 切换接口与冲突控制
- 切换流程中的解锁、确认、复位设置、二次确认逻辑

## macOS 开机自启

项目已经附带 `launchd` 管理脚本：

```bash
scripts/manage_launch_agent.sh
```

它会把服务安装为当前登录用户下的 `LaunchAgent`，而不是全局 `LaunchDaemon`。  
这更适合当前项目，因为它依赖：

- 用户目录下的 `.venv`
- 当前用户可访问的 `adb`
- 本地运行日志与运行时目录

### 1. 安装为自动启动服务

最常见用法：

```bash
APP_PASSWORD="你的访问密码" \
ADB_PATH="/Users/你的用户名/Library/Android/sdk/platform-tools/adb" \
scripts/manage_launch_agent.sh install
```

安装后会自动：

- 生成 `~/Library/LaunchAgents/com.guohai.esim-switchboard.plist`
- 加载并启动服务
- 设置为用户登录后自动拉起

默认访问地址：

```text
http://127.0.0.1:8000/
```

### 2. 修改配置并重载

例如改端口：

```bash
APP_PASSWORD="你的访问密码" \
ADB_PATH="/Users/你的用户名/Library/Android/sdk/platform-tools/adb" \
scripts/manage_launch_agent.sh reload --port 18000
```

`reload` 会重新生成 plist、重新加载服务并立即重启，适合在调整环境变量、日志路径、端口或 Python 路径后使用。

### 3. 查看服务状态

```bash
scripts/manage_launch_agent.sh status
```

### 4. 重启服务

```bash
scripts/manage_launch_agent.sh restart
```

### 5. 卸载服务

```bash
scripts/manage_launch_agent.sh uninstall
```

### 6. 预览或导出 plist

如果你想先看脚本最终生成的 `plist` 内容：

```bash
scripts/manage_launch_agent.sh print-plist
```

### 7. 日志位置

默认日志文件：

```text
~/Library/Logs/esim-switchboard.log
~/Library/Logs/esim-switchboard.err.log
```

### 8. 可传入的常用参数

- `--port 18000`
- `--host 127.0.0.1`
- `--label com.guohai.esim-switchboard`
- `--adb-path /absolute/path/to/adb`
- `--adb-device-serial 设备序列号`
- `--python-bin /absolute/path/to/.venv/bin/python`
- `--work-dir /absolute/path/to/project`
- `--db-path /absolute/path/to/db.sqlite`
- `--stdout-log /absolute/path/to/stdout.log`
- `--stderr-log /absolute/path/to/stderr.log`
- `--switch-screenshot-dir /absolute/path/to/runtime/switch_screenshots`
- `--switch-step-delay-seconds 1`
- `--switch-confirm-wait-seconds 10`

脚本也会把以下环境变量写入 `launchd` 配置：

- `APP_PASSWORD`
- `ADB_PATH`
- `ADB_DEVICE_SERIAL`
- `DB_PATH`
- `APP_AUTH_COOKIE_NAME`
- `ADB_HEALTHCHECK_TIMEOUT_SECONDS`
- `SMS_SYNC_DELAY_SECONDS`
- `ADB_RECONNECT_DELAY_SECONDS`
- `DEFAULT_PAGE_SIZE`
- `MAX_PAGE_SIZE`
- `SMS_EVENT_POLL_INTERVAL_SECONDS`
- `SWITCH_SCREENSHOT_DIR`
- `SWITCH_STEP_DELAY_SECONDS`
- `SWITCH_CONFIRM_WAIT_SECONDS`

脚本支持的命令一共是：

- `install`
- `reload`
- `restart`
- `status`
- `uninstall`
- `print-plist`

## 常见排障

### 1. 页面打开后一直提示需要密码

原因：

- 没有设置 `APP_PASSWORD`
- 输入密码不对
- 登录态 Cookie 已失效

处理：

```bash
export APP_PASSWORD="你的访问密码"
```

重启服务后重新登录。

### 2. ADB 不可用

现象：

- 页面 `ADB 可用` 显示 `NO`
- `/api/health` 返回 `adb_available=false`

处理：

```bash
"$ADB_PATH" devices
```

确认设备状态是 `device`，不是 `unauthorized`。

### 3. 页面没有短信

先手工验证：

```bash
"$ADB_PATH" shell content query --uri content://sms/inbox --projection address:body:sub_id:_id:date --sort "date DESC"
```

如果这里拿不到数据，说明问题在设备侧权限或 ROM 兼容性，而不是 Web 页面。

### 4. eSIM 切换卡住

优先查看切换蒙层里的最后一步截图。  
当前项目已经做了这些保护动作：

- 点亮屏幕后自动解锁
- 每次切换前强制退出设置 App，避免残留页面层级
- 动态匹配确认按钮文案
- 切换后等待 10 秒再次确认
- 用新的 `dumpsys isub` 结果确认是否切换成功

如果还是失败，可以结合：

- 页面最后一张截图
- 右侧步骤详情
- `runtime/switch_screenshots/` 下对应任务目录

一起排查。

### 5. 为什么页面显示“已切换成功”，但你还想进一步确认

当前成功判定不是只看页面截图，而是：

1. 等待 `SWITCH_CONFIRM_WAIT_SECONDS`
2. 再次截图
3. 同步一次本地 eSIM 快照
4. 再请求一次 `adb shell dumpsys isub`
5. 检查目标 `display_name` 是否已经出现在 `ActiveSubInfoList`

所以最终成功状态是相对可靠的。

## 开发说明

如果你后续要继续扩展，最关键的入口是：

- [app/main.py](/Users/guohai/Develop/esim-switch/app/main.py:1)：路由、鉴权、SSE、静态资源挂载
- [app/adb.py](/Users/guohai/Develop/esim-switch/app/adb.py:1)：ADB 命令和解析
- [app/switch_service.py](/Users/guohai/Develop/esim-switch/app/switch_service.py:1)：eSIM 切换与截图流程
- [app/templates/index.html](/Users/guohai/Develop/esim-switch/app/templates/index.html:1)：页面交互、密码层、切换蒙层
- [app/db.py](/Users/guohai/Develop/esim-switch/app/db.py:1)：短信查询和 SQLite 逻辑

## 当前限制

- 默认只处理 1 台 USB 设备
- 密码验证是轻量单密码方案，不是完整用户系统
- 暂不支持短信发送
- 不同 Android ROM 的设置页面文案、确认按钮和系统日志可能不同
- 某些设备的短信数据库访问或设置页面自动化会受系统限制
