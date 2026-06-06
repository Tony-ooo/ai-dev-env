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
│   ├── entrypoint.sh   - 容器启动入口脚本
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

## License

本项目采用 MIT License。
