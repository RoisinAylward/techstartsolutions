#!/bin/bash

echo "=== TechStart Solutions Security Monitor ==="
echo "Current username: $(whoami)"
echo "User ID: $(id -u)"
echo "Hostname: $(hostname)"
echo "Current date and time: $(date)"
echo "Number of logged-in users: $(who | wc -l)"
echo "Uptime: $(uptime)"
echo ""

check_service () {
    SERVICE_NAME=$1

    if systemctl is-active --quiet "$SERVICE_NAME"; then
        echo "$SERVICE_NAME is running"
    else
        echo "$SERVICE_NAME is NOT running"
        return 1
    fi
}

echo "=== Service checks ==="
check_service "ssh"
check_service "ufw"
check_service "cron"
check_service "fail2ban"
check_service "apache2"
echo""

check_file_permissions () {
    FILENAME=$1
    perms=$(stat -c %a "$FILENAME")
    echo "File: $FILENAME | Permissions: $perms"

    if [ "$FILENAME" == "/etc/shadow" ] && { [ "$perms" -ne 640 ] && [ "$perms" -ne 600 ]; }; then #-ne = not equal to
        echo "Shadow file may be insecure"
        return 1
    fi

    if [ "$FILENAME" != "/etc/shadow" ] && [ "$perms" -gt 644 ]; then #-gt = greater than
        echo "$FILENAME permissions may be too open"
        return 1
    fi
}

echo "=== Critical file permission checks ==="
for FILENAME in /etc/passwd /etc/shadow /etc/ssh/sshd_config; do
    check_file_permissions "$FILENAME"
done
echo ""

check_home_permissions () {
    user=$1
    if [ -d "/home/$user" ]; then
        echo "$user: Home directory exists"
        perms=$(stat -c %a "/home/$user")
        if [ "$perms" -gt 750 ]; then
            echo "$user's home directory has permissions: $perms"
            return 1
        fi
    else
        echo "$user: Home directory MISSING"
        return 1
    fi
}

echo "=== Home directory permission checks ==="
for user in azureuser dev1 dev2 qa1 qa2 pm1 pm2; do
    check_home_permissions "$user"
done
echo ""

echo "=== Suspicious activity within the last 30 minutes ==="
#checks failled ssh  attempts
FAILED_SSH=$(journalctl -u sshd --since "30 min ago" --no-pager 2>/dev/null | \
  grep -Eic "Failed password|Invalid user|authentication failure|Disconnected from authenticating user")
echo "Failed/invalid SSH attempts: $FAILED_SSH"

#checks fail2ban bans
if command -v fail2ban-client >/dev/null 2>&1; then
  BANNED=$(fail2ban-client status sshd 2>/dev/null | awk -F': ' '/Currently banned/ {print $2}')
  echo "Fail2ban currently banned IPs: ${BANNED:-0}"
fi

#checks blocks by ufw
UFW_BLOCKS=$(journalctl --since "30 min ago" --no-pager 2>/dev/null | grep -c "UFW BLOCK")
echo "UFW BLOCK events: $UFW_BLOCKS"

#checks sudo commands withtin last 30 mins
SUDO_EVENTS=$(journalctl -t sudo --since "30 min ago" --no-pager 2>/dev/null | wc -l)
echo "sudo events: $SUDO_EVENTS"
