#!/bin/bash
# ─────────────────────────────────────────────────────────────────
# Ethio House Realtor Bot - Deploy Script
# ─────────────────────────────────────────────────────────────────
# This script deploys the bot to Vercel with all required secrets.
#
# Prerequisites:
#   1. Install Vercel CLI: npm i -g vercel
#   2. Login to Vercel: vercel login
#   3. Fill in the secrets below
#   4. Run this script: bash deploy.sh
# ─────────────────────────────────────────────────────────────────

set -e

# ── Secrets (FILL THESE IN before running) ──
TELEGRAM_BOT_TOKEN=""  # Your bot token from @BotFather
GITHUB_PAT=""          # Your GitHub personal access token (for Gist state storage)
CONTACT_PHONE="0949024661"

if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$GITHUB_PAT" ]; then
    echo "❌ Error: Please fill in TELEGRAM_BOT_TOKEN and GITHUB_PAT in this script before running."
    exit 1
fi

echo "============================================"
echo "  Ethio House Realtor Bot - Deploy"
echo "============================================"
echo ""

# ── Step 1: Set Environment Variables ──
echo "Setting environment variables on Vercel..."

echo "$TELEGRAM_BOT_TOKEN" | vercel env add TELEGRAM_BOT_TOKEN production
echo "$TELEGRAM_BOT_TOKEN" | vercel env add TELEGRAM_BOT_TOKEN preview
echo "$TELEGRAM_BOT_TOKEN" | vercel env add TELEGRAM_BOT_TOKEN development

echo "$GITHUB_PAT" | vercel env add GITHUB_PAT production
echo "$GITHUB_PAT" | vercel env add GITHUB_PAT preview
echo "$GITHUB_PAT" | vercel env add GITHUB_PAT development

echo "$CONTACT_PHONE" | vercel env add CONTACT_PHONE production
echo "$CONTACT_PHONE" | vercel env add CONTACT_PHONE preview
echo "$CONTACT_PHONE" | vercel env add CONTACT_PHONE development

echo "Environment variables set!"
echo ""

# ── Step 2: Deploy to Production ──
echo "Deploying to Vercel production..."
vercel deploy --prod --yes
echo ""

# ── Step 3: Verify ──
DEPLOY_URL="https://ethio-house-realtor-bot.vercel.app"

echo "Verifying deployment at $DEPLOY_URL/api/status ..."
sleep 5
curl -s "$DEPLOY_URL/api/status" | python3 -m json.tool 2>/dev/null || curl -s "$DEPLOY_URL/api/status"
echo ""

echo ""
echo "============================================"
echo "  Deployment Complete!"
echo "============================================"
echo ""
echo "  Next Steps:"
echo ""
echo "  1. Initialize Gist state storage:"
echo "     Open: $DEPLOY_URL/api/init-gist"
echo ""
echo "  2. Set up cron-job.org (free):"
echo "     Go to: https://cron-job.org/en/signup/"
echo "     Create a job:"
echo "       URL: $DEPLOY_URL/api/rotate"
echo "       Method: GET"
echo "       Schedule: Every 3 hours"
echo "       (5 calls/day = 5 posts/day from different sites)"
echo ""
echo "  3. Test a manual post:"
echo "     Open: $DEPLOY_URL/api/rotate"
echo ""
echo "  Monitor: $DEPLOY_URL/api/status"
echo "  Reset daily: $DEPLOY_URL/api/reset-daily"
echo ""
