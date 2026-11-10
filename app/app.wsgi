#!/usr/bin/env python3
import sys
import os

# Adjust if you deploy under /var/www/flaskapp
APP_DIR = os.path.dirname(__file__)
sys.path.insert(0, APP_DIR)

from app import app as application
