import pythoncom
from wxbot_core import WXBot
from logger import log
log('INFO', 'manual_start_bot.py starting WXBot')
pythoncom.CoInitialize()
try:
    bot = WXBot()
    bot.run()
finally:
    pythoncom.CoUninitialize()
