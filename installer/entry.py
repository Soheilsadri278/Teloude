"""PyInstaller entry point for the packaged application.

The application lives in the ``app`` package and ``app/__main__.py`` imports it relatively
(``from .infrastructure...``). That only works when the module is imported *as part of the
package*. Handing PyInstaller ``app/__main__.py`` runs it as a bare script, where ``__package__``
is empty and Python raises, before anything else happens::

    ImportError: attempted relative import with no known parent package

PyInstaller could not resolve those relative imports either, so it collected no application
modules at all - the bundle looked complete and simply never started. Booting here instead means
``app`` is imported as the package it is, both by the frozen build and by ``python installer/entry.py``.
"""

from __future__ import annotations

from app.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
