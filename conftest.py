# Чтобы `pytest` находил пакет app и из корня репозитория.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
