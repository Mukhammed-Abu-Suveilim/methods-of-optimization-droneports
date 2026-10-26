"""
Совместимый запуск: compare_routing.py; результаты относятся к модели
v2.
"""

import runpy
import sys
from pathlib import Path


def main() -> None:
    """
    Передать прежнюю команду общему расчёту без записи результатов v1.
    """
    # 1. Сохранить выбор экземпляра для прежних solve-команд.
    arguments = ["routing"]
    # 2. Выполнить общий CLI через стандартный механизм Python.
    sys.argv = ["run_project.py", *arguments]
    runpy.run_path(
        str(Path(__file__).with_name("run_project.py")), run_name="__main__"
    )


if __name__ == "__main__":
    main()
