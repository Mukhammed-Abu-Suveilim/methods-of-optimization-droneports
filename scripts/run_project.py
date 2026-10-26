"""Совместимый прямой запуск команд пакета scripts."""

import sys
from pathlib import Path


def main(arguments: list[str] | None = None) -> None:
    """Настроить путь прямого запуска и передать управление CLI."""
    # 1. Поддержать запуск файлом вне механизма python -m.
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)

    # 2. Импортировать пакет после настройки его корневого каталога.
    from scripts.commands import main as execute

    execute(arguments)


if __name__ == "__main__":
    main(sys.argv[1:])
