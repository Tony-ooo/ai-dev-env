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
└── .gitignore     - Git 忽略规则
```

## 快速开始

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

## License

本项目采用 MIT License。
