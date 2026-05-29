import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__))))
from bot.db.users import get_or_create_user

try:
    print(get_or_create_user(99999, 'testuser999'))
except Exception as e:
    import traceback
    traceback.print_exc()
