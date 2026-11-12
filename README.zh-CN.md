# TrackNest

[English](README.md) | 简体中文

基于 Trackio 的个人与团队实验工作空间，支持自行部署、成员权限和训练日志上报。管理员通过邀请链接注册新用户；用户在 Panel 中管理个人实验和团队项目。

## 部署

要求 Linux、Python 3.10+ 和 Nginx。以下命令在项目目录中执行，始终使用同一个系统账号。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/flask --app app init-db
.venv/bin/flask --app app create-admin admin
sudo cp nginx/manager.conf /etc/nginx/conf.d/trackio-manager.conf
sudo nginx -t && sudo systemctl reload nginx
./start.sh
./status.sh
```

管理员密码至少 12 位。打开 <http://localhost:9000/login>，远程访问时替换服务器地址。仅需开放 `9000`；管理后端 `9001` 和实验实例 `9002–9141` 均监听 `127.0.0.1`。

```bash
./stop.sh                                      # 停止管理服务和全部实验实例，保留数据
./start.sh --storage-dir /mnt/trackio-storage   # 可选：自定义存储根目录
./status.sh --lang zh                          # 脚本默认英文，支持中文和 --help
```

默认存储在项目目录的 `instance/` 和 `data/`。自定义路径记录在 `.storage.json`，重启时仍需传入相同的 `--storage-dir`，否则恢复默认路径。首次选择空目录后，先执行上面的 `init-db`、`create-admin`，再用相同参数启动。切换已有存储位置需先停机并自行复制数据；`MANAGER_STATE_DIR`、`MANAGER_DATA_ROOT` 环境变量优先于保存的配置。

## 训练接入

在训练机器上访问服务器；需要 SSH 转发时，保持以下隧道运行：

```bash
ssh -N -L 9000:localhost:9000 <服务器>
```

登录网页，从个人或团队空间复制 URL 和账号 Token。使用训练环境的 Python 安装客户端：

```bash
curl --noproxy '*' -fLO http://localhost:9000/client/tracknest_client-0.1.8-py3-none-any.whl
python -m pip install --no-deps --upgrade ./tracknest_client-0.1.8-py3-none-any.whl
tracknest login --server-url http://localhost:9000/u/<用户名>/
```

直接访问服务器时替换域名；团队空间使用 `/p/<项目ID>/`。按提示输入 Token，连接保存在 `~/.config/tracknest/client.json`（支持 `XDG_CONFIG_HOME`，权限 `0600`）。切换空间或重置 Token 后重新登录。一个账号的 Token 通用于个人与团队空间，权限由项目成员身份决定。

**客户端不强制安装依赖，不会升级现有环境中的包。** 训练和业务 CLI 需同一环境中已有可用的 Trackio SDK（已验证 `0.45.0`）；缺少时明确提示。导入、`--help` 和 `--version` 无需 SDK。旧安装产生的依赖冲突需另行处理。

```python
import tracknest as trackio

trackio.init(project="my-project", name="baseline")
for step in range(25):
    trackio.log({"loss": 1 / (step + 1)}, step=step)
trackio.finish()
```

`init()` 自动启动实例，`finish()` 等待上传但不停止实例。断网时保留本地缓存，下次初始化同一 project 重试。客户端只对 TrackNest 请求绕过代理，不修改训练环境的代理变量。使用前取消 `TRACKIO_SPACE_ID`；原生 `import trackio` 不读取客户端连接配置。

```bash
tracknest status                  # 连接、实例状态和本地待传缓存
tracknest start                   # 启动已配置的实例
tracknest stop                    # 停止实例，影响其所有训练上报
tracknest projects                # 远端项目列表
tracknest runs my-project        # 项目中的 Run
tracknest status --project <团队项目名或ID>
tracknest status --lang zh        # 默认英文
tracknest --version
tracknest --help
```

也可用 `python -m tracknest` 指定 Python 环境。`status/start/stop` 支持 `--project`，不修改已保存的训练连接；同名团队项目使用 ID。团队所有者和编辑者可启动实例，仅所有者可停止。停止前先暂停训练上报。

## 项目管理

- 在 Panel 创建团队项目，邀请已注册用户并分配只读或编辑权限。
- 共享已有个人项目：暂停训练、停止个人实例，在「管理实验项目」中选择「邀请并转为团队项目」。数据保留，所有者原来的个人 URL 和项目名仍可使用。
- 永久删除个人项目：停止个人实例后，在「管理实验项目」中输入完整项目名确认。删除团队项目或账号则保留实验文件。
- 管理员可修改用户名；改名前需停止该用户的个人实例和其拥有的团队实例，改名后更新个人上报 URL。昵称可由用户自行修改。

## 数据与维护

| 路径 | 内容 |
| --- | --- |
| `instance/manager.db` | 账号、权限、Token 和实例信息 |
| `instance/secret.key` | 会话签名密钥 |
| `instance/gunicorn-*.log` | 管理服务日志 |
| `data/<用户名>/` | 个人实验和实例日志 |
| `data/<创建者>/projects/<项目ID>/` | 团队实验和实例日志 |

实例日志名为 `server.log`。备份前暂停训练并执行 `./stop.sh`，将实际存储位置的 `instance/` 和 `data/` 一并复制到项目之外，保留符号链接；同时保存 `.storage.json`（如有）。备份包含登录凭据。

**升级：** 停机备份并保存本地改动后执行：

```bash
git pull --ff-only
.venv/bin/pip install -r requirements.txt
./start.sh    # 自定义存储时带上原 --storage-dir 参数
./status.sh
```

启动时自动执行支持的数据库升级，无需重新创建管理员。Nginx 配置有变动时重新安装并检查、重载；客户端有更新时在训练环境中重新安装 wheel。

**整套迁移：** 停机后复制完整 `instance/`、`data/` 到新机器的空部署，保留符号链接。部署相同版本代码、重建 `.venv` 并配置 Nginx，再使用对应存储参数启动。启动脚本自动适配数据路径和失效 PID；保留原 `manager.db`、`secret.key`，不要覆盖已有部署。

**单用户迁移：** 源端先停止该用户的个人实例和其拥有的团队实例，再导出；复制完整备份目录到目标部署后导入：

```bash
# 源端
.venv/bin/flask --app app export-user alice /tmp/alice-backup
# 目标端
.venv/bin/flask --app app import-user /tmp/alice-backup
```

备份包含 `user.json`、账号凭据、个人数据和该用户创建的团队项目。导入拒绝覆盖已有账号或冲突数据，账号按普通用户恢复，团队成员需重新邀请；指向用户目录外的符号链接会被拒绝。迁移后更新训练端服务器地址，外部路径和文件另行处理。

**卸载：** 停机并完成所需备份后，删除 `/etc/nginx/conf.d/trackio-manager.conf`，检查并重载 Nginx，再删除项目目录。项目外的存储和备份需自行清理。

## 界面资源

可复用的组件、主题和布局位于 [`tracknest-ui/`](tracknest-ui/)，使用方式见其 [`SKILL.md`](tracknest-ui/SKILL.md)。
