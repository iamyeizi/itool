"""Persistencia local y no sensible de preferencias de interfaz."""

import json
import os
import platform


def _settings_path():
    if platform.system().lower() == 'windows':
        state_dir = os.path.join(os.getenv('LOCALAPPDATA', os.path.expanduser('~')), 'iTool')
    else:
        state_dir = os.path.join(os.getenv('XDG_STATE_HOME', os.path.expanduser('~/.local/state')), 'itool')
    return os.path.join(state_dir, 'ui_settings.json')


def load_ui_settings(settings_path=None):
    try:
        with open(settings_path or _settings_path(), encoding='utf-8') as settings_file:
            settings = json.load(settings_file)
        return settings if isinstance(settings, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save_ui_settings(settings, settings_path=None):
    settings_path = settings_path or _settings_path()
    try:
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        temporary_path = f'{settings_path}.tmp'
        with open(temporary_path, 'w', encoding='utf-8') as settings_file:
            json.dump(settings, settings_file, sort_keys=True)
        os.replace(temporary_path, settings_path)
    except OSError:
        pass
