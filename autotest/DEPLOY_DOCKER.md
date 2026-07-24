# 服务器 Docker 部署文档

适用于任意一台装了 Docker 的 Linux 服务器，不依赖任何面板。

## 前置条件

- Docker Engine ≥ 20.10，且带 Compose Plugin：
  ```bash
  docker --version
  docker compose version   # 没有的话装 docker-compose-plugin
  ```
- 服务器网络能访问到被测的目标系统（内网系统要保证服务器在同一网络/能连上 VPN）
- 至少 1 核 2G，跑 Chromium 建议 2G 以上内存

## 部署步骤

```bash
# 1. 把代码放到服务器，进入项目目录
git clone <你的仓库地址>
cd autotest

# 2. 配置账号密码
mkdir -p runtime
cp .env.example runtime/.env
vi runtime/.env
# AUTOTEST_USER / AUTOTEST_PASS —— 被测系统的登录账号
# WEB_USER / WEB_PASS          —— 控制台自己的访问口令，务必配置，
#                                  不配的话任何人打开 5000 端口都能操作

# 3. 构建并后台启动
docker compose up -d --build

# 4. 看日志确认启动成功
docker compose logs -f autotest
```

启动后浏览器打开 `http://服务器IP:5000`，没登录会自动跳到登录页，填 `WEB_USER`/`WEB_PASS` 登录后才能进入 Web 控制台。

## 登录口令

`WEB_USER`/`WEB_PASS` 是控制台本身的访问口令，跟被测系统的账号是两回事。没配置的话服务端不做任何校验、直接能访问，启动时会在日志里打警告——**部署到公网服务器前一定要配置**，否则任何人拿到地址就能操作、看报告、跑测试。

登录态是服务端 session，存在浏览器 cookie 里，关闭浏览器不会马上失效；右上角有「退出登录」。session 签名密钥每次启动随机生成，所以**重启容器/进程后需要重新登录一次**——不想每次重启都要重登的话，在 `.env` 里加一行 `SECRET_KEY=一串随机字符串` 固定下来。改完 `.env` 记得 `docker compose restart autotest` 才生效。

## 目录挂载说明

`docker-compose.yml` 里挂载了几样东西，都是宿主机持久化数据，重建容器不会丢：

| 宿主机路径      | 容器内路径         | 作用                                     |
|-----------------|--------------------|-------------------------------------------|
| `./configs`     | `/app/configs`     | 用例配置，改了不用重新 build             |
| `./projects`    | `/app/projects`    | 系统/菜单/勾选状态——**漏挂载这个的话，每次 `--build` 重建容器，之前建的系统和勾选都会被清空**，因为这些数据只写在容器内部的可写层里 |
| `./reports`     | `/app/reports`     | 执行报告                                 |
| `./runtime`     | `/app/runtime`     | 账号密码 + 控制台口令目录                |
| `./auth`        | `/app/auth`        | 登录态目录，容器重启不用重登             |

## 防火墙 / 安全组

放行 `5000` 端口（云服务器还要在控制台安全组里放行，不只是服务器本机防火墙）：

```bash
# ufw 示例
ufw allow 5000/tcp
```

如果不想直接暴露 5000，建议在前面套一层 Nginx/Caddy 做反向代理 + HTTPS（1Panel 环境可以直接看 [DEPLOY_1PANEL.md](DEPLOY_1PANEL.md)，用面板自带的网站功能，不用手写 Nginx 配置）。

## 不用 docker compose 的等价命令

```bash
docker build -t autotest .
docker run -d --name autotest \
  -p 5000:5000 \
  -v $(pwd)/configs:/app/configs \
  -v $(pwd)/projects:/app/projects \
  -v $(pwd)/reports:/app/reports \
  -v $(pwd)/runtime:/app/runtime \
  -v $(pwd)/auth:/app/auth \
  -e TZ=Asia/Shanghai \
  --shm-size=1gb \
  --restart unless-stopped \
  autotest
```

`--shm-size` 必须设置（compose 里已经设了 `1gb`），否则 Chromium 渲染复杂页面时容易崩溃（`/dev/shm` 默认只有 64M）。页面特别重、报错 `Target closed`/`Page crashed` 的话可以调到 `2gb`。

## 验证码 / 无法自动登录的场景

如果目标系统登录页有验证码，容器里跑不了自动登录，需要在**有图形界面的机器**（比如你自己的电脑）上先手动登录一次生成 `auth/state.json`：

```bash
python cli.py login <登录页地址>
```

然后把生成的 `auth/state.json` 传到服务器项目目录覆盖同名文件（`scp`/`rsync` 均可），容器会直接复用这份登录态。

## 升级

```bash
git pull
docker compose up -d --build
```

`configs/`、`projects/`、`reports/`、`.env`、`auth/` 都在宿主机上，重新构建镜像不影响这些数据（前提是都按上面的表挂载了卷，尤其别漏了 `projects/`）。

## 常用运维命令

```bash
docker compose logs -f autotest      # 实时日志
docker compose restart autotest      # 重启
docker compose down                  # 停止并删除容器（数据不受影响）
docker compose exec autotest bash    # 进容器排查问题
```
