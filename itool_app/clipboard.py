"""Copiado compatible con Windows, WSL y escritorios Linux."""

import platform
import shutil
import subprocess


def copy_text(root, value):
    """Copia texto sin pasarlo como argumento de un proceso externo."""
    text = str(value)
    if platform.system().lower() == 'linux' and shutil.which('clip.exe'):
        try:
            subprocess.run(
                ['clip.exe'],
                input=text,
                text=True,
                check=True,
                timeout=5,
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass

    root.clipboard_clear()
    root.clipboard_append(text)
    root.update()
