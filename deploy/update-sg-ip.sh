#!/bin/zsh
# ──────────────────────────────────────────────────────────────
# Auto-update AWS Security Group with current public IP
# Run manually:  ./deploy/update-sg-ip.sh
# Or via cron:   */30 * * * * /path/to/update-sg-ip.sh
# ──────────────────────────────────────────────────────────────

SG_ID="sg-0ef135d5637888767"
REGION="ap-south-1"
PORTS=(22 80 9090)
DESCRIPTION="AlgoForge-AutoIP"
IP_FILE="$HOME/.algoforge_last_ip"

# Get current public IPv4
CURRENT_IP=$(curl -4 -s --max-time 5 ifconfig.me 2>/dev/null)
if [[ -z "$CURRENT_IP" || ! "$CURRENT_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "[$(date '+%H:%M:%S')] ❌ Could not detect public IP"
  exit 1
fi

# Read last known IP
LAST_IP=""
[[ -f "$IP_FILE" ]] && LAST_IP=$(cat "$IP_FILE")

# No change? Exit silently.
if [[ "$CURRENT_IP" == "$LAST_IP" ]]; then
  echo "[$(date '+%H:%M:%S')] ✅ IP unchanged: $CURRENT_IP"
  exit 0
fi

echo "[$(date '+%H:%M:%S')] 🔄 IP changed: ${LAST_IP:-none} → $CURRENT_IP"

# Remove old IP rules (if any)
if [[ -n "$LAST_IP" ]]; then
  for PORT in "${PORTS[@]}"; do
    AWS_PAGER="" aws ec2 revoke-security-group-ingress \
      --group-id "$SG_ID" --region "$REGION" \
      --protocol tcp --port "$PORT" --cidr "${LAST_IP}/32" 2>/dev/null
  done
  echo "[$(date '+%H:%M:%S')] 🗑️  Removed old IP: $LAST_IP"
fi

# Add new IP rules
PERM_ARGS=()
for PORT in "${PORTS[@]}"; do
  PERM_ARGS+=("IpProtocol=tcp,FromPort=$PORT,ToPort=$PORT,IpRanges=[{CidrIp=${CURRENT_IP}/32,Description=$DESCRIPTION}]")
done

AWS_PAGER="" aws ec2 authorize-security-group-ingress \
  --group-id "$SG_ID" --region "$REGION" \
  --ip-permissions "${PERM_ARGS[@]}" 2>/dev/null

if [[ $? -eq 0 ]]; then
  echo "$CURRENT_IP" > "$IP_FILE"
  echo "[$(date '+%H:%M:%S')] ✅ Security group updated: $CURRENT_IP (ports: ${PORTS[*]})"
else
  echo "[$(date '+%H:%M:%S')] ⚠️  AWS update failed (IP may already exist)"
  echo "$CURRENT_IP" > "$IP_FILE"
fi
