"""Keep the existing Typer commands available without importing the old checkout."""

from apps.cli import app


if __name__ == "__main__":
    app()
