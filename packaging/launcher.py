"""PyInstaller entry point for both packaged executables."""

from personal_assistant.main import main

if __name__ == "__main__":
    raise SystemExit(main())
