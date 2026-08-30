* You can find the code for NVWave at: https://github.com/nvaccess/nvda/blob/master/source/nvwave.py
* The code for Say all in NVDA is at: https://github.com/nvaccess/nvda/blob/master/source/speech/sayAll.py
* the speech manager at: https://github.com/nvaccess/nvda/blob/master/source/speech/speech.py
* And speech without pauses: https://github.com/nvaccess/nvda/blob/master/source/speech/speechWithoutPauses.py
* And the NVDA addon development guide at: https://github.com/nvdaaddons/DevGuide/wiki/NVDA-Add-on-Development-Guide
* And recent changes for developers here: https://github.com/nvaccess/nvda/blob/master/user_docs/en/changes.md
Always ensure this synth driver is behaving as expected by the above NVDA code and documentation.

## Local Python development environment

- Use the workstation's system-installed 64-bit Python for dependency
  installation, tests, linting, and add-on builds. Its executable is:
  `C:\Users\nick\AppData\Local\Programs\Python\Python313\python.exe`.
- Codex's sandboxed shell may report that `python` is unavailable and may also
  block direct access to that executable. This does not mean Python is missing.
  Invoke the absolute path with the required sandbox escalation instead of
  searching for, downloading, installing, or substituting another interpreter.
- Do not use a pinned Codex runtime, `.python32`, or the repository `.venv`
  unless the user explicitly requests one.
