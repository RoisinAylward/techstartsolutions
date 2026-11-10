#!/bin/bash
set -e
DEST="/var/backups/webapp"
mkdir -p "$DEST"
tar -czf "$DEST/webapp-$(date +%F-%H%M).tar.gz" /home/azureuser/webapp
