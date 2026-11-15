# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 revimg contributors

"""Entry point for the frozen executable.

``revimg/__main__.py`` uses a relative import, which is correct for
``python -m revimg`` but fails when PyInstaller runs it as a top-level script.
Keeping a separate absolute-import entry point makes both paths work.
"""

from revimg.cli import main

raise SystemExit(main())
