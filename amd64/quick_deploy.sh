#!/bin/bash

# ==================================================
# 参数使用说明:
#   USER_NAME       - 用户名（示例: --user-name alice）
#   PORT_BASE       - 端口基数，用于生成唯一端口（示例: --port-base 30）
#   USER_PASSWORD   - 用户密码（示例: --user_password 'abc123'）
#   BASE_DATA_DIR   - 基础数据目录（示例: BASE_DATA_DIR="/new/path"）
#   CPUS            - CPU 核心数限制（空表示不限制）（示例: --cpu 4）
#   MEMORY          - 内存大小限制（空表示不限制）（示例: --memory 8g）
#   ENABLE_HTTPS    - 启用 HTTPS（true/false，默认 false）（示例: ENABLE_HTTPS="true"）
# ==================================================

echo "=================================================="
echo "Starting Docker image deployment process..."
echo "Image: tonyooo/ai-dev-env:3.0-amd64"
echo "GPU: auto-detect"
echo "=================================================="

USER_NAME=tony
PORT_BASE=30
USER_PASSWORD=$(openssl rand -base64 32 | tr -d '=+/.' | cut -c1-32)

# 自定义参数
BASE_DATA_DIR="D:/ProgramData/my_projects/DockerSpace/ai-cloud-station/user-data"
IMAGE_NAME="tonyooo/ai-dev-env:3.0-amd64"
ENABLE_HTTPS="false"

# 执行部署脚本，GPU 自动检测
bash --login -c "../src/custom_deploy.sh \
--user-name $USER_NAME \
--port-base $PORT_BASE \
--user_password $USER_PASSWORD \
--base_data_dir $BASE_DATA_DIR \
--image $IMAGE_NAME \
--enable-https $ENABLE_HTTPS"
DEPLOY_STATUS=$?

echo
echo "============================================"
if [ $DEPLOY_STATUS -ne 0 ]; then
    echo "❌ Deployment script failed with error code $DEPLOY_STATUS."
else
    echo "✅ Deployment script executed successfully."
fi

echo "============================================"
read -n 1 -s -r -p "Task finished. Press any key to exit."
