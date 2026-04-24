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

