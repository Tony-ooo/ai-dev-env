#!/bin/bash

echo "============================================"
echo "Starting Docker image push process..."
echo "Image: tonyooo/ai-dev-env:4.0-arm64"
echo "============================================"

# 推送 Docker 镜像
docker push tonyooo/ai-dev-env:4.0-arm64
PUSH_STATUS=$?

echo
echo "============================================"
if [ $PUSH_STATUS -ne 0 ]; then
    echo "❌ Docker push failed with error code $PUSH_STATUS."
else
    echo "✅ Docker push completed successfully."
fi

echo "============================================"
read -n 1 -s -r -p "Task finished. Press any key to exit."

