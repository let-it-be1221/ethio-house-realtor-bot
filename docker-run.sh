#!/bin/bash

# Ethio House Realtor Bot - Docker Deployment

echo "Building Docker image..."
docker build -t ethio-house-realtor-bot .

echo "Running bot container..."
docker run -d \
    --name ethio-realtor-bot \
    --restart unless-stopped \
    --env-file .env \
    ethio-house-realtor-bot

echo "Bot is running! Use 'docker logs -f ethio-realtor-bot' to view logs."
