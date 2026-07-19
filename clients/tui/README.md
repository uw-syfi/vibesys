# VibeSys TUI

Terminal client and launcher for VibeSys.

```bash
npm install -g @vibesys/tui
vibesys-tui --help
```

The launcher starts the Python VibeSys backend with `python -m vibesys.cli` and
then attaches the OpenTUI client. Install the Python `vibesys` package in the
Python environment you want to use, or set `VIBESYS_PYTHON` to that Python
executable.
