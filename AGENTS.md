# Project Agent Instructions

## Environment

- **Python interpreter**: `C:\analytics\projects\git\lexi\demos\venv\Scripts\python.exe`

---

## Code Editing Policy

### Syntax-check after every Python edit

After editing any `.py` file, immediately verify it parses cleanly before committing:

```powershell
& "C:\analytics\projects\git\lexi\demos\venv\Scripts\python.exe" -m py_compile path\to\edited_file.py
```

A `SyntaxError` or `IndentationError` at module level causes a completely silent crash when the process runs in a background window — stderr is invisible, the process dies before any log call, and the only symptom the user sees is a timeout.

### `safe_local_imports` — blast-radius limitation

Any Python file with an `if __name__ == "__main__":` block that imports non-stdlib modules must centralise those imports in a single function named `safe_local_imports`:

```python
def safe_local_imports(g: dict) -> None:
    """Load all non-stdlib local-module imports into *g* (pass globals()).

    Centralising imports here limits blast radius: if any import raises
    (e.g. a SyntaxError or ImportError buried in an imported module), the
    exception is caught, logged with a full traceback, then re-raised —
    so the log always contains a FATAL line before the process dies.

    Call once near the top of main():
        safe_local_imports(globals())
    """
    try:
        from mymodule import MyClass         # <- replace with this file's actual imports
        g["MyClass"] = MyClass
        # ... all other non-stdlib imports ...
    except Exception:
        import traceback as _tb
        _log(f"FATAL: safe_local_imports failed:\n{_tb.format_exc()}")
        raise


def main() -> int:
    safe_local_imports(globals())
    # MyClass etc. are now available as module globals
    ...
```

**Scope:**
- Same-repo local imports only.
- Cross-repo symbols loaded via `versholn.importx()` are already wrapped separately — do not duplicate them here.
- stdlib imports do not belong here.

**Why "blast radius":** A `SyntaxError` or `ImportError` buried inside an imported module kills the process before `_log` is even defined. Without `safe_local_imports`, a hidden-window process exits with no log entry. With it, there is always at least one `FATAL` line in the log.

