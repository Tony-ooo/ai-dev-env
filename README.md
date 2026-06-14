# AI Cloud Station Dev Env

AI 云站开发环境 Docker 镜像构建配置。

## 项目结构

```
├── amd64/          # AMD64 架构构建脚本
│   ├── build.sh        - 构建 Docker 镜像
│   ├── push_image.sh   - 推送镜像到仓库
│   └── quick_deploy.sh - 快速部署脚本
├── arm64/          # ARM64 架构构建脚本
│   ├── build.sh        - 构建 Docker 镜像
│   ├── push_image.sh   - 推送镜像到仓库
│   └── quick_deploy.sh - 快速部署脚本
├── src/            # Docker 镜像源文件
│   ├── Dockerfile      - Docker 镜像构建文件
│   ├── entrypoint.sh   - s6-overlay 容器初始化脚本
│   └── custom_deploy.sh - 自定义部署脚本
└── README.md            - 项目说明文档
```

## 快速开始

### 构建参数设置

```bash
# AMD64 架构
cd amd64 && cp build.sh.example build.sh
# 修改 build.sh 中的构建参数

# ARM64 架构
cd arm64 && cp build.sh.example build.sh
# 修改 build.sh 中的构建参数
```

### 构建镜像

```bash
# AMD64 架构
cd amd64 && ./build.sh

# ARM64 架构
cd arm64 && ./build.sh
```

### 推送镜像

```bash
# AMD64 架构
cd amd64 && ./push_image.sh

# ARM64 架构
cd arm64 && ./push_image.sh
```

### 快速部署

```bash
# AMD64 架构
cd amd64 && ./quick_deploy.sh

# ARM64 架构
cd arm64 && ./quick_deploy.sh
```

默认部署时，SSH 和临时测试服务端口仅绑定到宿主机 `127.0.0.1`，避免局域网或外网扫描产生异常登录日志。如需远程访问，请在部署脚本中显式设置 `--bind-address 0.0.0.0`，并配合防火墙白名单限制来源 IP。

镜像使用 s6-overlay 作为容器 1 号主进程。容器初始化阶段会以 root 执行用户 UID/GID、密码、SSH 与挂载目录权限配置；通过 SSH 登录后仍进入 `dev` 用户。如需通过 `docker exec` 进入 `dev` 用户，请使用 `docker exec -u dev -it <container> bash`。

### 创建容器内部系统守护进程

容器运行后，可以在容器内部创建 s6 管理的守护进程。服务定义保存在 `/etc/services.d/<service-name>/run`，同一个容器重启后会由 s6 自动拉起。

```bash
sudo mkdir -p /etc/services.d/my-service
sudo tee /etc/services.d/my-service/run >/dev/null <<'EOF'
#!/command/with-contenv bash
exec /path/to/your-daemon --foreground
EOF
sudo chmod +x /etc/services.d/my-service/run

# 重启容器后，s6 会自动启动该服务
docker restart <container>
```

守护进程必须以前台方式运行，不要在 `run` 脚本里使用 `&` 后台启动。若删除容器并重新创建，容器内部手动创建的服务文件会丢失；需要长期保留时，建议写入 Dockerfile 或从宿主机挂载到 `/etc/services.d/`。

## License

本项目采用 MIT License。
