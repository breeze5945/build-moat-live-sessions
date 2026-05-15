import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncio
from task_scheduler.server import main

asyncio.run(main())
