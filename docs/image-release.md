# 镜像发布维护

每台构建机需要安装 Docker、GitHub CLI，并登录 Docker Hub 和具有仓库 Actions 写入权限的 GitHub 账号。首次在构建机上执行：

```bash
docker login
gh auth login
```

在 GitHub 仓库的 **Settings → Secrets and variables → Actions** 中配置两个仓库密钥：

- `DOCKERHUB_USERNAME`：拥有镜像仓库推送权限的 Docker Hub 用户名。
- `DOCKERHUB_TOKEN`：该账号具有镜像推送权限的访问令牌。

密钥只在 GitHub 页面输入，不要写入源码或聊天。发布工作流需要先合入默认分支。

在 AMD64 和 ARM64 机器上分别检出相同的 `main` 提交，保持工作区干净并执行：

```bash
scripts/build.sh --with-browser --push
```

推送成功后脚本自动请求 GitHub Actions 检查该版本。四个单架构镜像全部就绪且版本、源码提交一致时，工作流生成开发环境和浏览器镜像的多架构版本标签，并更新 `latest`。其他架构尚未就绪时，工作流等待下一次推送触发，不更新标签。版本默认按 `main` 第一父提交历史从第一个提交开始编号，需要完整 Git 历史；各构建机必须使用相同的 `RELEASE_BASE` 和 `RELEASE_SERIES`。

如果通知失败，镜像可能已经推送；重新执行原命令会复用已发布的相同版本镜像并重试通知。若工作流失败，可在 GitHub Actions 查看失败步骤并重跑。单架构发布和 GitHub Actions 更新 `latest` 都应避免同仓库的其他外部发布流程并发写入。两个镜像仓库不能同时原子更新；部署脚本会检查已标记版本的开发和浏览器镜像是否匹配。
