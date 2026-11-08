# TrackNest Docker Compose 部署方案

日期：2026-11-05。本文只做分析，不实施容器化、不运行测试或部署命令，也不调整现有服务。

## 结论与前提

**可以使用 Docker Compose 部署。建议先做小范围容器化适配，通过镜像内回归、双容器集成、数据迁移和停机恢复测试后，再切换正式服务。**

本方案按以下前提设计：单台 Linux 服务器、一个管理应用副本、SQLite 和实验文件存放在本机持久化目录，只对外提供 9000 入口。继续保留每位用户的 Panel、管理员邀请注册、项目邀请和成员退出功能。

宿主机环境的完整测试已通过 **92 项**，包括真实 Nginx 代理、存储路径选择、启停脚本、整套部署拷贝和单用户导入导出。这是现有功能的回归基线，**尚未构建或验证 Docker 镜像**；本文的 Docker 部署操作仍未执行。

目前仓库没有 Dockerfile 或 Compose 配置，因此还不能直接执行 `docker compose up` 部署。

## 推荐结构

```text
浏览器 / 训练 SDK
        │
   宿主机 :9000
        │
 gateway 容器：Nginx :9000
        │ Compose 内部网络，访问 manager:9001
 manager 容器：Gunicorn 0.0.0.0:9001
        ├─ 用户 Trackio 子进程：127.0.0.1:9002–9141
        ├─ 项目 Trackio 子进程：127.0.0.1:9002–9141
        └─ 挂载持久化的 instance/ 和 data/
```

| 服务 | 职责 | 宿主机端口 | 持久化数据 |
| --- | --- | --- | --- |
| `gateway` | 登录限流、统一入口、代理请求 | 仅映射 `9000:9000` | 只读挂载 Nginx 配置 |
| `manager` | 登录鉴权、成员管理、创建和管理 Trackio 子进程 | 不映射端口 | 读写挂载管理状态和实验目录 |

这是根据当前进程模型得出的建议：`app.py` 使用 `subprocess.Popen` 启动后端，通过本机回环地址访问，并读取 `/proc/<pid>/` 验证身份。让这些进程留在同一个应用容器中，可以保留现有管理逻辑；无需把每个项目改成独立容器，也无需挂载 Docker Socket。

两个服务通过服务名通信，不使用固定容器 IP。这与 Compose 的服务发现方式一致。[Docker Compose 网络说明](https://docs.docker.com/compose/how-tos/networking)

## 实施前需要完成的适配

| 项目 | 当前情况 | 建议处理 |
| --- | --- | --- |
| 应用镜像 | 依赖本机 `.venv` | 新增 Dockerfile，固定已验证的 Python 基础镜像，安装 `requirements.txt`，包含 `app.py`、`storage_config.py`、`script_output.py`、`data_transfer.py`、`personal_projects.py`、`trackio_server.py`、`templates/` 和 `static/` |
| 构建上下文 | `.gitignore` 不能代替 Docker 构建排除规则 | 新增 `.dockerignore`，排除 `.venv`、实验、管理数据库、备份、凭据和运行日志；保留 `tracknest-ui/assets/` |
| Gunicorn | `start.sh` 使用 `--daemon` 和 PID 文件 | 容器内以前台模式运行，保持 `--workers 1 --threads 4 --timeout 60`，绑定 `0.0.0.0:9001` |
| Nginx 上游 | `127.0.0.1:9001` | 新增容器专用配置，改为 `manager:9001`，保留 Host、限流、上传大小及代理超时配置 |
| 容器入口 | 暂无 | 新增入口脚本，负责正常启动时的进程状态整理、信号处理及有序停机 |
| Compose | 暂无 | 定义两个服务、持久化挂载、健康检查、启动依赖、退出等待时间及重启策略 |
| 运维方式 | 三个脚本管理宿主机进程 | 宿主机脚本继续保留；容器部署使用 Compose 命令，避免混用两套进程管理方式 |

容器内不直接调用现有 `start.sh`。后台化会让容器的主命令提前结束；同时，现有 `status.sh` 假设 Nginx 和管理进程共享本机网络，在双容器结构中不能原样充当健康检查。Docker 的主进程与子进程管理原则可参考[官方多进程说明](https://docs.docker.com/engine/containers/multi-service_container/)。

镜像使用有权限读写数据的非 root 用户，并为其准备可写的 HOME / 缓存目录。部署时核对宿主机目录的 UID、GID 和权限，不通过把数据目录设为全员可写来解决权限问题。无需特权模式或宿主机 PID 命名空间。

## 数据保存与已有数据迁移

### 新部署

代码可放在容器的 `/app`，状态和实验文件分别挂载到固定目录，例如 `/storage/instance`、`/storage/data`，并设置：

```text
MANAGER_STATE_DIR=/storage/instance
MANAGER_DATA_ROOT=/storage/data
```

新项目仍保存为 `/storage/data/<创建者>/projects/<项目ID>/`，协作者访问同一份数据。数据不能只写入容器可写层，否则重建容器时无法保留。[Docker 持久化存储说明](https://docs.docker.com/engine/storage/)

### 当前部署迁移：适配容器内路径

现有管理数据库的 `TrackioUser.data_dir`、`SharedProject.data_dir` 保存了绝对路径。项目现已提供 `relocate-data`：在数据拷贝完成、实验实例停止后，将这些路径适配到当前 `MANAGER_DATA_ROOT`，修复内部兼容链接并清理失效 PID。宿主机 `start.sh` 会自动执行适配；未来的容器入口应显式调用此命令，再前台启动 Gunicorn。

因此，可以将当前数据目录挂入统一的容器路径：

| 宿主机路径 | 容器路径 | 用途 |
| --- | --- | --- |
| `<path-to>/tracknest/instance` | `/storage/instance` | 管理数据库和签名密钥 |
| `<path-to>/tracknest/data` | `/storage/data` | 实验数据库、媒体、日志和兼容链接 |

环境变量使用容器内路径。须先在完整数据副本上验证适配、曲线、媒体和文件访问，再切换正式数据；不要让宿主机原服务和容器同时操作同一份目录。bind mount 的宿主机目录与容器目录可以不同。[Docker bind mount 说明](https://docs.docker.com/engine/storage/bind-mounts/)

迁移前完整停止旧管理服务和全部 Trackio 实例，再备份整个 `instance/` 与 `data/`，保留符号链接。尤其要保留 `manager.db` 和 `secret.key`，不能用空目录覆盖它们。先在备份副本上验收，正式切换时禁止宿主机旧进程和容器同时写同一份数据。

持久化目录继续使用本机文件系统。当前 SQLite、文件锁和本地进程管理方式不作为多主机共享存储方案使用。

## 容器生命周期：必须验证的部分

### 启动与 PID 整理

- 正常启动应用容器时，在 Gunicorn 接受请求前整理上次容器遗留的实例 PID 和旧管理 PID 文件。容器重启后，数据库中的 PID 不能直接视为本次运行中的进程。
- 第一版保持“实验实例按需手动启动”，不新增自动恢复全部项目的功能；保留账号、端口、成员、Token 和实验文件。
- PID 整理只允许发生在明确的容器启动阶段，不能放进通用 `init_db()`，也不能在执行管理员、迁移等维护命令时无条件清空 PID。
- 管理员创建仍为一次性交互操作，不在镜像中写入默认密码，不在每次启动时重建管理员。空数据库初始化与已有数据升级应区分处理。

### 有序停止

`start_trackio()` 使用 `start_new_session=True` 创建独立进程组。因此，不能假设给 Gunicorn 发出 SIGTERM 就能让全部 Trackio 实例正常退出。

容器入口应接收停止信号：先让管理服务停止接收新操作并处理在途请求，再按现有身份校验逻辑停止 Trackio 实例、更新 PID，最后退出。设置 `init: true` 辅助信号转发和回收孤儿进程，但它不能替代上述应用清理。Compose 的 `stop_grace_period` 应覆盖实际停机耗时；当前后端逐个停止的最坏耗时会随实例数量增加，不能未经压测就承诺固定时间内全部优雅退出。[Compose init 与停止等待配置](https://docs.docker.com/reference/compose-file/services/)

还应测试强制终止后的 SQLite 恢复。容器退出后会结束其中剩余进程，但这不等于已经完成应用层的正常停机。

### 重启与副本数量

只运行 **一个 manager 服务副本**。当前 PID、端口分配和实例管理依赖本机进程视图，不能通过 `--scale manager=2` 直接扩容。9002–9141 共 140 个端口由用户和项目共同占用，已停止的实例仍保留端口；容器化不会改变该容量限制。

可采用 `restart: unless-stopped` 恢复异常退出的容器，但它不会替代项目实例的按需启动，也不应被理解为自动修复所有健康检查失败。[Docker 重启策略](https://docs.docker.com/engine/containers/start-containers-automatically/)

manager 重建后内部 IP 可能变化。升级流程应确保 gateway 重新解析上游；第一版可在更新应用后同步重建 gateway，并验证请求恢复，避免保留旧连接或地址造成 502。[Compose 容器更新与网络连接](https://docs.docker.com/compose/how-tos/networking)

## 状态检查与日志

| 检查层级 | 检查内容 |
| --- | --- |
| manager 健康检查 | 本容器请求 `127.0.0.1:9001/login`，显式带公开入口的 Host，禁止跟随重定向，必须返回 200 |
| gateway 健康检查 | 请求本容器的 `127.0.0.1:9000/login`，必须返回 200，验证代理到管理应用的链路 |
| 用户 / 项目状态 | 检查数据库登记的进程身份；运行中的实例再检查 `/version`，主动停止不算故障 |
| 外部验收 | 从浏览器及训练机器访问实际 9000 地址，验证登录、面板、静态资源和 SDK 上报 |

当前代码会将 Host 端口为 9001 的请求重定向到 9000；健康检查如果跟随重定向，可能把网关可用性误当成后端可用性。因此需要单独验证 manager 和 gateway。

gateway 使用 `depends_on: condition: service_healthy` 等待 manager 就绪；这主要解决初次启动顺序，不代表运行期间的持续故障修复。[Compose 启动顺序说明](https://docs.docker.com/compose/how-tos/startup-order/)

Gunicorn 和 Nginx 的访问、错误日志输出到 stdout / stderr，由 Compose 查看并设置日志轮转。Trackio 的 `server.log` 继续位于持久化数据目录，需要单独控制文件增长。健康检查和日志不输出成员 Token、密码或签名密钥。

## 测试与上线门槛

以下测试均为后续待执行项。本次没有运行 Docker 或测试命令。

| 阶段 | 需要验证的内容 | 通过标准 |
| --- | --- | --- |
| 镜像构建 | 固定依赖安装、Trackio 启动器、非 root 文件权限、UI 资源完整 | 镜像可启动，无宿主机 `.venv` 或凭据依赖 |
| 原有回归 | 在测试镜像中运行完整现有测试，包括真实 Nginx 测试 | 基线 92 项通过，不因缺少 Nginx 而跳过集成项；新增项也全部通过 |
| 双容器链路 | 通过 gateway 访问 `/u/`、`/p/`、静态资源及上传接口 | 无 404 / 502，Host、登录限流和来源校验正常 |
| 注册和团队项目权限 | 管理员邀请注册；创建者邀请已注册成员；编辑、只读、退出、移除与 Token 撤销 | 与现有行为一致，退出或移除后无法继续访问或写入 |
| 实验记录 | SDK 上报 25 个 iteration 点，读取 loss / accuracy，并访问媒体文件 | 曲线横轴和点数正确，文件可读取，跨用户数据隔离 |
| 容器生命周期 | 正常 stop / up、restart、重建、强制终止，以及多个运行实例下的停机 | 无残留实例和错误 PID 识别；重启后可按需启动，无数据库损坏 |
| 持久化 | down / up 后检查账号、成员、Token、实验、密钥和文件 | 挂载数据完整，删除容器不导致实验丢失 |
| 旧数据迁移 | 在独立副本上挂载到新的容器路径并执行 `relocate-data` | 账号及实验记录保留，曲线、媒体和 `_shared` 兼容链接可用 |
| 更新与回滚 | 更新 manager 镜像并恢复 gateway；恢复旧镜像及匹配的数据备份 | 入口恢复，旧版本能读取恢复后的数据，不依赖未经验证的降级兼容 |

现有测试会在临时目录中创建数据。测试镜像需要单独安装 pytest 和 Nginx，生产 manager 镜像无需内置 Nginx。其中 `test_scripts.py` 测试宿主机脚本并依赖项目下 `.venv`；容器测试阶段需准备该环境或有针对性地调整测试夹具，不能直接删除这项测试。现有 Nginx 集成测试在同一测试环境里启动进程，也不能代替新增的跨容器链路测试。

上线门槛是：上述阶段全部通过，并完成一次备份恢复演练；不能只以镜像构建成功或登录页返回 200 作为验收结论。

## 后续运维方式与切换顺序

未来配置完成后，日常命令可以简化为以下形式；**目前没有 Compose 文件，这些命令尚不可直接用于本项目部署**：

```bash
docker compose up -d       # 启动
docker compose ps         # 容器及健康状态
docker compose logs -f    # 服务日志
docker compose stop       # 停止，保留数据
```

`docker compose down` 可用于移除容器和网络；数据必须依赖前述持久化挂载保留。采用 named volume 时，`down -v` 会删除声明的卷，不应纳入日常停机步骤。[Compose down 的删除范围](https://docs.docker.com/reference/cli/docker/compose/down)

正式切换建议按顺序进行：

1. 完成容器化文件与生命周期适配，在临时数据和测试端口上通过全部验收。
2. 完整停止当前管理应用和实验实例，备份 `instance/` 与 `data/`，记录待恢复的项目。
3. 释放宿主机 Nginx 占用的 9000 监听。若它还服务其他网站，只调整 Trackio 对应配置，不停止整个 Nginx。
4. 使用已验收镜像和正式数据挂载启动 Compose，核对目录权限和数据，再通过 9000 验证登录、团队项目权限及实验记录。
5. 如需回滚，先完整停止容器，再按备份恢复数据和旧入口。切换后若已有新写入，需先保留新数据并处理差异，不能直接用旧备份覆盖。

本轮交付仅为此评估文档。Dockerfile、Compose 配置、入口脚本及容器验收均待后续实施。
