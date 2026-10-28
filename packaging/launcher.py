"""PyInstaller entry point for both packaged executables."""

import multiprocessing

from personal_assistant.main import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
