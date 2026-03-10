#!/usr/bin/with-contenv bashio

bashio::log.info "Starting OzBargain Deal Scanner..."

# Export config options as environment variables
export CHECK_INTERVAL_MINUTES=$(bashio::config 'check_interval_minutes')
export MIN_UPVOTES=$(bashio::config 'min_upvotes')
export MIN_DISCOUNT_PERCENT=$(bashio::config 'min_discount_percent')
export NOTIFY_SERVICE=$(bashio::config 'notify_service')
export USE_HA_SHOPPING_LIST=$(bashio::config 'use_ha_shopping_list')
export HA_TODO_ENTITY=$(bashio::config 'ha_todo_entity')
export SMART_KEYWORD_EXPANSION=$(bashio::config 'smart_keyword_expansion')
export NOTIFICATION_COOLDOWN_HOURS=$(bashio::config 'notification_cooldown_hours')
export MAX_DEALS_PER_NOTIFICATION=$(bashio::config 'max_deals_per_notification')
export DEAL_SCORE_THRESHOLD=$(bashio::config 'deal_score_threshold')

# Custom shopping list items (JSON array)
export CUSTOM_SHOPPING_LIST=$(bashio::config 'custom_shopping_list')
export EXCLUDED_KEYWORDS=$(bashio::config 'excluded_keywords')

# Home Assistant connection
export HA_TOKEN="${SUPERVISOR_TOKEN}"
export HA_URL="http://supervisor/core"

# Ingress path
export INGRESS_PATH=$(bashio::addon.ingress_entry)

bashio::log.info "Check interval: ${CHECK_INTERVAL_MINUTES} minutes"
bashio::log.info "Notification service: ${NOTIFY_SERVICE}"
bashio::log.info "Ingress path: ${INGRESS_PATH}"

cd /app
exec python3 -u src/main.py
