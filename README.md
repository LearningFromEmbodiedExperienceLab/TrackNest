# Trackio 多用户管理

所有用户通过同一个登录页进入自己的 Panel，创建项目、邀请已注册成员并查看实验。管理员在“用户管理”中生成新用户的邀请注册链接；项目成员可自行退出共享项目。

## 首次安装

要求 Linux、Python 3.10+ 和 Nginx。以下命令均在 `/home/syw/trackio_manager` 中执行，并使用同一个系统账号运行。

```bash
cd /home/syw/trackio_manager
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

后台启动：

```bash
./start.sh
```

打开 <http://localhost:9000/login>；远程访问时将 `localhost` 换为服务器地址。登录后在 Panel 创建项目，在项目页面启动实例、邀请成员并获取训练接入示例和 Token。

| 用途 | 监听地址 |
| --- | --- |
| 浏览器及训练上报的统一入口 | `9000` |
| 管理后端 | `127.0.0.1:9001` |
| 用户及项目的 Trackio 实例 | `127.0.0.1:9002–9141` |

只需对外开放统一入口。创建项目或用户后无需修改 Nginx 配置。

## 查看状态和日志

```bash
# 检查管理后端、9000 入口及全部用户和项目实例
./status.sh

# 查看管理服务日志，Ctrl+C 退出查看
tail -f instance/gunicorn-error.log
```

`status.sh` 在两个 HTTP 入口正常、没有意外退出的实例时返回退出码 0，否则返回 1；主动停止的实验实例不算异常。请求记录在 `instance/gunicorn-access.log`，每个实验实例的启动和报错日志在其数据目录下的 `server.log`。自定义 `MANAGER_STATE_DIR` 时，管理 PID 和日志也保存在该目录中。

## 数据保存位置

默认根目录为 `/home/syw/trackio_manager`：

| 路径 | 内容 |
| --- | --- |
| `instance/manager.db` | 账号、邀请、项目、成员权限、Token 和实例信息 |
| `instance/secret.key` | 登录会话签名密钥 |
| `data/<创建者>/projects/<项目ID>/` | 项目的实验数据库、媒体文件和 `server.log` |
| `data/<用户名>/` | 原有个人实例的实验数据库和 `server.log` |

项目数据始终保存在创建者目录中，协作者访问同一份数据。停止实例或删除项目、账号都会保留实验数据；删除项目后不会自动重新关联其数据。旧项目迁移后，`data/_shared/` 下仅保留兼容历史文件路径的符号链接。

备份时先按下方步骤完整停机，再一并复制 `instance/` 和 `data/`，其中包含访问凭据，请妥善保管。如需更改存储位置，可在首次初始化前设置 `MANAGER_STATE_DIR` 和 `MANAGER_DATA_ROOT`；后续启动和维护命令须使用相同环境变量，已有数据不会自动搬迁。

## 停止和重启

完整停止管理服务和全部用户、项目实验实例，保留数据：

```bash
./stop.sh
```

首次切换到脚本时，若此前手动启动了 Gunicorn，先在原终端按 Ctrl+C 并等待退出，再运行 `stop.sh` 清理实验实例，然后使用 `start.sh`。脚本不会自动停止来源不明的 9001 端口进程。Nginx 由系统单独管理，脚本不会启停它。

重启时依次运行 `./stop.sh`、`./start.sh`。已停止的实验实例需要登录后手动启动。三个脚本均可从任意工作目录调用，请使用同一个系统账号及相同的存储环境变量。
