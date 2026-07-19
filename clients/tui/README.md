# VibeSys TUI

Terminal client and launcher for VibeSys.

```bash
npm install -g @vibesys/tui
vs --help
```

The package installs `vs` and `vibesys` as aliases for the same launcher. The
launcher starts the Python VibeSys backend with `python -m vibesys --headless`
and then attaches the OpenTUI client. Install the Python `vibesys` package in
the Python environment you want to use, or set `VIBESYS_PYTHON` to that Python
executable.
