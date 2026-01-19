#!/bin/bash

echo "============================================"
echo "Starting Docker image build process..."
echo "Image: tonyooo/ai-dev-env:3.0-amd64"
echo "============================================"

# 构建 Docker 镜像
docker build -t tonyooo/ai-dev-env:3.0-amd64 ../src
BUILD_STATUS=$?

echo
echo "============================================"
if [ $BUILD_STATUS -ne 0 ]; then
    echo "❌ Docker build failed with error code $BUILD_STATUS."
else
    echo "✅ Docker image built successfully."
fi

echo "============================================"
read -n 1 -s -r -p "Task finished. Press any key to exit."

