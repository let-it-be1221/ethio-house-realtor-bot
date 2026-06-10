#!/bin/bash

# Ethio House Realtor Bot - Startup Script

echo "====================================="
echo "  Ethio House Realtor Telegram Bot  "
echo "====================================="
echo ""

# Check if .env exists
if [ ! -f .env ]; then
    echo "ERROR: .env file not found!"
    echo "Please create a .env file with the following variables:"
    echo "  TELEGRAM_BOT_TOKEN=your_bot_token"
    echo "  TELEGRAM_CHANNEL=@your_channel"
    echo "  CONTACT_PHONE=your_phone_number"
    exit 1
fi

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt --quiet

# Run the bot
echo ""
echo "Starting the bot..."
echo "Press Ctrl+C to stop"
echo ""
python main.py
