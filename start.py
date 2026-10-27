"""Deployment launcher. Keeps bot.py unchanged and reads the token from BOT_TOKEN."""
import builtins
import os
import sys

token = os.environ.get('BOT_TOKEN', '').strip()
if not token or ':' not in token:
    raise SystemExit('Set the BOT_TOKEN environment variable in your hosting dashboard.')

builtins.input = lambda prompt='': token
import bot
bot.main()
