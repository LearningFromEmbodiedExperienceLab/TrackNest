# TrackNest

[English](README.md) | 简体中文

基于 Trackio、可自行部署的个人与团队实验工作空间。

所有用户通过同一个登录页进入自己的 Panel，创建团队项目、邀请已注册成员并查看实验。管理员在“用户管理”中生成新用户的邀请注册链接，并在结果页点击“复制链接”；项目成员可自行退出团队项目。

管理员可在用户管理中修改自己及其他用户的用户名，邀请新用户时可填写昵称。用户点击页头账号名可修改自己的昵称；项目邀请和成员列表会显示“用户名（昵称）”。修改用户名之前需停止该用户的个人实例及其拥有的团队项目；改名会迁移数据目录，保留密码、Token 和成员权限，之后需更新个人实验上报 URL。

## 复用界面设计

设计资源集中在 [`tracknest-ui/`](tracknest-ui/)：`VERSION` 记录当前版本 `1.0.0`，`CHANGELOG.md` 记录变更，`assets/` 包含组件、布局、明暗主题、主题脚本、图标和独立 HTML 示例，`references/` 保存规范与参考图。TrackNest 通过 `/static/ui/` 直接加载该目录的资源；`templates/` 保留业务页面。

把整个 `tracknest-ui/` 复制到个人 skills 目录后，可在新项目中使用 `$tracknest-ui`。Skill 会指导复制 `assets/` 与版本文件，再接入新项目自己的内容和交互；其他项目不会自动跟随升级。详细步骤见 [`SKILL.md`](tracknest-ui/SKILL.md)。预览时可在该包的 `assets/` 目录运行 `python -m http.server 9188 --bind 127.0.0.1`，打开 `example.html`（组件）或 `workspace.html`（布局）。

## 首次安装

要求 Linux、Python 3.10+ 和 Nginx。以下命令均在 `<path-to>/tracknest` 中执行，并使用同一个系统账号运行。

```bash
cd <path-to>/tracknest
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/flask --app app init-db
.venv/bin/flask --app app create-admin admin
```

按提示设置至少 12 位的管理员密码。安装 Nginx 配置（只需一次）：

```bash
sudo cp nginx/manager.conf /etc/nginx/conf.d/trackio-manager.conf
sudo nginx -t && sudo systemctl reload nginx
```

## 启动应用

后台启动，不传存储参数时使用项目目录下的 `instance/` 和 `data/`：

```bash
./start.sh
```

也可以指定持久化根目录，管理状态存入其 `instance/`，实验数据存入其 `data/`：

```bash
./start.sh --storage-dir /mnt/trackio-storage
```

相对路径以项目目录为基准，带空格的路径请加引号。该选择保存在项目本机的 `.storage.json` 中，`stop.sh`、`status.sh` 和 Flask 维护命令会读取相同配置；它不进入 Git。重启自定义部署时需再次传入相同的 `--storage-dir`，不传则恢复项目默认路径。已有 `MANAGER_STATE_DIR` / `MANAGER_DATA_ROOT` 环境变量仍可覆盖保存的配置，请保持操作环境一致。

首次选择空目录时，脚本会记录路径并提示尚未初始化；随后执行“首次安装”中的 `init-db`、`create-admin` 两条命令，再按相同参数启动。已有部署切换位置时，先完整停机，将 `instance/`、`data/` 拷贝到指定根目录后再启动；脚本不会自动搬动原文件。查看参数帮助可运行 `./start.sh --help`。

打开 <http://localhost:9000/login>；远程访问时将 `localhost` 换为服务器地址。登录后在 Panel 创建团队项目，在项目页面启动实例、邀请成员并获取训练接入示例和 Token。

| 用途 | 监听地址 |
| --- | --- |
| 浏览器及训练上报的统一入口 | `9000` |
| 管理后端 | `127.0.0.1:9001` |
| 用户及项目的 Trackio 实例 | `127.0.0.1:9002–9141` |

只需对外开放统一入口。创建团队项目或用户后无需修改 Nginx 配置。

## 通过 SSH 转发上报训练

在训练机器保持隧道运行，例如 `ssh -N -L 9000:localhost:9000 PCL-4090-syw`，然后访问 <http://localhost:9000/login>，登录后获取个人或团队空间的 URL 和 Token。个人空间支持多个 project；更换 `trackio.init(project=...)` 的名称时，上报 URL 和 Token 不变。

保持 SSH 隧道连接，在任意目录下载服务器提供的客户端 wheel，使用训练 Python 安装并配置一次连接（直接访问服务器时替换域名；团队空间使用页面显示的 `/p/<项目ID>/` URL）：

```bash
curl --noproxy '*' -fLO http://localhost:9000/client/tracknest_client-0.1.7-py3-none-any.whl
python -m pip install --upgrade ./tracknest_client-0.1.7-py3-none-any.whl
tracknest login --server-url http://localhost:9000/u/<用户名>/
```

按提示输入页面上的 Token。配置保存在 `~/.config/tracknest/client.json`（支持 `XDG_CONFIG_HOME`），文件权限为 `0600`；切换空间或重置 Token 后重新运行 login。无需每次设置 URL、Token 或 NO_PROXY。

每个账号只有一个客户端 Token，个人空间和全部团队项目共用同一个 Token；是否可读写某个团队项目由成员权限决定。重置账号 Token 会使该账号在所有空间中的旧凭据失效。旧版本的项目专属 Token 不再用于鉴权，已有团队接入请使用页面显示的账号 Token 重新登录。

`login` 由管理端验证凭据，实验实例停止时也能成功登录，且不会启动实例。训练调用 `tracknest.init()` 时会自动启动已停止的实例，等待就绪后再初始化训练日志；查询实验数据前可先执行 `tracknest start`。`status` 会分别显示鉴权结果和实例运行状态；`finish()` 不会关闭实例。

训练脚本只需把导入改为 `import tracknest as trackio`，其余 `init/log/finish` 代码保持原样。之后可从任意目录直接运行 `python test.py`，无需进入仓库目录。客户端只对已配置的 TrackNest 连接使用 `trust_env=False`，不修改 HTTP/SOCKS 代理环境变量，不影响训练下载或其他请求。

终端状态日志使用英文，带蓝色 `tracknest:` 前缀：连接和进度为青色，成功为绿色，待传提醒为黄色，上传错误为红色。重定向输出或设置 `NO_COLOR` 时自动关闭颜色。日志包含连接验证、已上传的指标条数、结束时的同步等待及结果。服务器不可连接或 Token 无效时，初始化直接报错；训练中断网时保留 Trackio 的本地缓存，并明确提示同步未完成，下次初始化同一 project 时重试。服务器确认的条数是本次进程累计的上报指标条数，也可能包含重传的缓存。

未使用薄客户端的原生 `import trackio` 仍按 Trackio 自身规则工作：没有远端 URL 就只写本地，也不会自动读取上述配置。若设置了 `TRACKIO_SPACE_ID`，需先取消它以使用 TrackNest。管理端对本机实例的启动检查和转发请求会绕过环境代理；实例未运行时返回 503，凭据或权限错误仍返回 401/403。

## 客户端状态查看与实例管理

安装后使用同一训练环境中的命令，无需进入仓库目录：

```bash
tracknest status                  # 配置路径、连接鉴权、实例状态、本地待传缓存
tracknest start                   # 启动当前配置的实例并等待就绪
tracknest stop                    # 关闭当前配置的实例，影响该实例的所有训练上报
tracknest projects                # 当前空间内的远端 project 列表
tracknest runs my-project-test    # Run 名称、ID 和创建时间
tracknest status --lang zh        # 使用中文输出，默认英文
tracknest --version               # 查看已安装客户端版本
tracknest --help
```

保持已保存的个人或团队训练连接不变，也可通过团队项目名称或 ID 管理指定实例：

```bash
tracknest status --project team-baseline
tracknest start --project team-baseline
tracknest stop --project team-baseline  # 暂停实例并保留数据，仅所有者可执行
```

`--project` 用于 `status`、`start`、`stop` 选择团队项目实例，不修改登录配置，也不会改变训练 `init()` 中的实验项目名。访问权限由已保存的账号 Token 决定，实例未启动时也能选择。若多个可访问项目同名，CLI 会列出所有者和 ID，请使用 `--project <ID>`；不加该参数时仍操作已保存登录地址对应的实例。此功能需同时更新服务端和客户端。

也支持 `python -m tracknest status` 等同名命令，以明确使用指定的 Python 环境。查询只读，继续保留训练代理，不显示 Token。`status` 即使无法连接服务器，也会检查当前服务器对应的本地缓存；缓存数量不包含仍在训练进程内存中的上传队列。配置缺失、连接或查询失败、`status` 中实例未运行、缓存无法完整读取时退出码为 1；正常查询为 0，存在待传缓存时显示提醒。`start` 和 `stop` 操作已保存登录 URL 对应的实例，可重复执行。个人实例使用本人的账号 Token 启停；团队实例允许所有者和编辑者启动（含训练自动启动），仅所有者可关闭，只读成员只能查询状态。执行 `stop` 前应暂停训练上报，因为它会影响使用该实例的所有 Run。启停命令不重传缓存，初始化同一 project 时会重试待传数据。使用新功能需同时更新服务端和客户端。

终端输出统一配色：鉴权已验证、实例运行中为绿色；实例已停止、未能验证为红色。标题、字段名、路径、表格和提示也使用颜色；重定向输出或设置 `NO_COLOR` 时保留纯文本。每次命令输出前后各留一行空白。

CLI 默认英文，在任意子命令前后加 `--lang zh` 可使用中文，例如 `tracknest --lang zh projects` 或 `tracknest start --lang zh`。状态页按组展示连接信息，并用对齐表格汇总待上传项目和数量；项目列表及 Run 列表也使用表格。实例停止时，查询会在连接 SDK 前提示先执行 `tracknest start`；HTTP 和连接失败使用简短说明，不再直接显示 Space 报错和 HTTP 文档链接。

可用 `tracknest --version` 核对训练环境实际安装的版本。如果仍提示从网页启动实例，请使用上面的 wheel 安装命令，使用训练所用的同一个 Python 升级到 `0.1.7`。服务端也需更新，以提供新版 wheel 和实例管理接口。

## 查看状态和日志

```bash
# 检查管理后端、9000 入口及全部用户和项目实例
./status.sh

# 查看管理服务日志，Ctrl+C 退出查看
tail -f instance/gunicorn-error.log
```

`status.sh` 在两个 HTTP 入口正常、没有意外退出的实例时返回退出码 0，否则返回 1；主动停止的实验实例不算异常。请求记录在 `instance/gunicorn-access.log`，每个实验实例的启动和报错日志在其数据目录下的 `server.log`。自定义 `MANAGER_STATE_DIR` 时，管理 PID 和日志也保存在该目录中。

## 邀请协作者共享个人项目

在 Panel 的「个人实验空间 → 管理实验项目」中展开项目，选择已注册的协作者和只读或编辑权限，点击「邀请并转为团队项目」。操作前先暂停训练并停止个人实例。项目名需符合现有团队项目命名规则，且不能与自己已有的团队项目重名。

只会转换选中的 project，保留已有 Run、指标、媒体、产物和 Trace 数据；其他个人项目保持私有。项目会出现在「团队项目」中，可继续添加或移除成员、调整权限。启动团队实例后协作者使用团队 URL 和自己的账号 Token 访问。

所有者原来的个人上报 URL、项目名和账号 Token 继续可用，无需修改训练接入；使用原 URL 时重新启动个人实例即可，新增数据与团队实例共享同一份存储。转换时保留了用户数据目录内部的相对路径链接，备份和迁移用户数据时请保留这些链接。

## 删除个人实验项目

在 Panel 的「个人实验空间」中点击「管理实验项目」。先暂停训练上报并停止个人实例，再展开要删除的项目，输入完整项目名，确认「永久删除项目数据」。这会清除该项目的 Run、指标、媒体、产物和本地 Trace 数据，不影响其他个人项目或团队项目。之后重新上报同名项目会再次创建它。

此操作与「删除团队项目」不同：后者撤销成员权限，但保留实验文件。

## 数据保存位置

默认根目录为 `<path-to>/tracknest`：

| 路径 | 内容 |
| --- | --- |
| `instance/manager.db` | 账号、邀请、项目、成员权限、Token 和实例信息 |
| `instance/secret.key` | 登录会话签名密钥 |
| `data/<创建者>/projects/<项目ID>/` | 项目的实验数据库、媒体文件和 `server.log` |
| `data/<用户名>/` | 原有个人实例的实验数据库和 `server.log` |

项目数据始终保存在创建者目录中，协作者访问同一份数据。停止实例或删除项目、账号都会保留实验数据；删除项目后不会自动重新关联其数据。旧项目迁移后，`data/_shared/` 下仅保留兼容历史文件路径的符号链接。

备份时先按下方步骤完整停机，再一并复制实际存储位置的 `instance/` 和 `data/`，其中包含访问凭据，请妥善保管。可通过启动参数选择统一根目录，也可用 `MANAGER_STATE_DIR` 和 `MANAGER_DATA_ROOT` 分别指定路径；已有文件不会自动搬迁。

## 跨机器迁移

### 整套部署迁移

1. 先停止训练上报，在旧机器运行 `./stop.sh`，确认管理服务和全部实验实例已停止。
2. 将完整的 `instance/` 和 `data/` 拷贝到新机器的项目目录或所选存储根目录中，保留符号链接。自定义存储位置时，拷贝脚本打印出的实际目录，并在新机器使用 `--storage-dir` 或相应环境变量选择位置。`manager.db` 与 `secret.key` 都要保留。
3. 在新机器部署相同版本的项目代码，重新创建 `.venv`、安装 `requirements.txt`，按“首次安装”配置 Nginx；不要复制旧机器的虚拟环境，也不需要重新创建管理员。确认新系统账号有权读写拷贝来的文件。
4. 运行 `./start.sh`、`./status.sh`。启动脚本会将管理数据库中的数据路径适配到新位置，将内部兼容链接改成相对链接，并清理失效的 PID；原有账号、密码、团队成员、Token 和实验数据保留。实验实例可通过网页、`tracknest start` 或训练初始化时按需启动。

整套迁移用于空部署；不要用旧 `manager.db` 覆盖另一套已有账号的部署。可先拷贝到备份副本中验证。部署目录和数据目录可以不同于旧机器；启动前也可手动运行 `.venv/bin/flask --app app relocate-data` 完成路径适配。内部实例端口仍使用原分配值，新机器需要确保这些端口未被其他程序占用。

### 单个用户迁移到已有部署

源机器先停止该用户的个人实例和其拥有的全部团队项目，再导出到新的备份目录：

```bash
.venv/bin/flask --app app export-user alice /tmp/alice-backup
```

将整个 `alice-backup/` 目录拷贝到目标机器，在目标部署目录中执行：

```bash
.venv/bin/flask --app app import-user /tmp/alice-backup
```

只支持已注册用户。备份包含账号密码哈希、Token、个人实验和该用户创建的团队项目；导入后仍使用原密码及本人的 Token，内部账号 ID 和端口重新分配。导入账号为普通用户，实例保持停止。其他用户拥有的项目不会随之迁移，也不会按同名用户名自动恢复团队成员权限；由所有者在目标部署重新邀请成员。

导入遇到同名账号、已有用户目录、团队项目 ID 或成员 Token 冲突时会拒绝覆盖。不能只拷贝 `data/alice/` 就恢复账号和团队权限，必须一并保留导出的 `user.json`。备份包含登录凭据，请限制访问；目录内指向外部文件的符号链接会拒绝导出或导入，需先将所需文件整理到用户数据目录内。

两种迁移都需要将训练端的服务器地址改为新机器地址；尚未使用的邀请注册链接也需更换主机地址。实验配置中自行填写的外部绝对路径和远端文件引用不属于本地数据目录，需要另行处理。

## 停止和重启

完整停止管理服务和全部用户、项目实验实例，保留数据：

```bash
./stop.sh
```

首次切换到脚本时，若此前手动启动了 Gunicorn，先在原终端按 Ctrl+C 并等待退出，再运行 `stop.sh` 清理实验实例，然后使用 `start.sh`。脚本不会自动停止来源不明的 9001 端口进程。Nginx 由系统单独管理，脚本不会启停它。

重启时依次运行 `./stop.sh`、`./start.sh`；自定义存储部署的启动命令需带上原 `--storage-dir` 参数。已停止的实验实例可通过网页、`tracknest start` 或训练初始化时启动。三个脚本均可从任意工作目录调用，运行时会打印当前管理数据目录和实验数据目录；请使用同一个系统账号及相同的存储环境变量。

脚本默认输出英文；添加 `--lang zh` 输出中文（启动时可与 `--storage-dir` 一起使用）：

```bash
./start.sh --lang zh
./stop.sh --lang zh
./status.sh --lang zh
```

终端颜色：青色为路径及提示，绿色为成功或运行中，黄色为已停止或提醒，红色为错误或异常。重定向输出时自动关闭颜色，也可设置 `NO_COLOR=1` 禁用颜色。三个脚本都支持 `--help`。

## 拉取更新和升级

在实际运行 TrackNest 的机器上操作，使用与启动时相同的系统账号和存储环境变量。请将 `/path/to/tracknest` 替换为实际项目目录。

1. 检查本地改动；需要保留的修改请先提交或另行保存：

   ```bash
   cd /path/to/tracknest
   git status --short
   ```

2. 暂停训练上报，停止应用：

   ```bash
   ./stop.sh
   ```

   确认停机成功后，将脚本打印的实际状态目录和数据目录备份到项目目录之外，保留符号链接。备份需包含 `manager.db`、`secret.key` 和实验文件；如有 `.storage.json`，也请保留。

3. 拉取代码并更新依赖。每条命令成功后再继续；如果 Git 提示本地改动冲突或分支分叉，先处理再升级：

   ```bash
   git pull --ff-only
   .venv/bin/pip install -r requirements.txt
   ```

4. 使用原来的存储参数启动并检查状态。默认存储位置执行：

   ```bash
   ./start.sh
   ./status.sh
   ```

   自定义存储位置则改用下方命令，并将路径替换为原有存储根目录：

   ```bash
   ./start.sh --storage-dir /mnt/trackio-storage
   ./status.sh
   ```

启动时会执行已支持的管理数据库升级。保留原数据和签名密钥，无需重新创建管理员；实验实例按需从 Panel 启动。如果新版本修改了 Nginx 配置，请按“首次安装”更新已安装的配置并检查、重载 Nginx。

## 卸载

请将下方 `/path/to/tracknest` 替换为实际项目目录。

1. 使用与启动时相同的系统账号和存储环境变量停止应用：

   ```bash
   cd /path/to/tracknest
   ./stop.sh
   ```

   如需保留账号和实验数据，删除前先将脚本打印的状态目录和数据目录拷贝到项目目录之外，保留符号链接，并确认包含 `manager.db` 和 `secret.key`。

2. 如果安装了本文的 Nginx 配置，删除该配置并重载：

   ```bash
   sudo rm -- /etc/nginx/conf.d/trackio-manager.conf
   sudo nginx -t && sudo systemctl reload nginx
   ```

3. 确认停机及所需备份完成后，永久删除项目目录，其中的代码、`.venv` 和数据也会一并删除：

   ```bash
   rm -rf -- /path/to/tracknest
   ```

项目目录之外的存储目录和备份会保留；如需清除这些数据，请另行删除。无需卸载系统 Python 或 Nginx。
