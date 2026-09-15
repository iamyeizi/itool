import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from itool_app import networking
from itool_app.remote_desktop import normalize_rdp_username
from itool_app.settings import load_ui_settings, save_ui_settings
from itool_app.ui_components import format_status_text


class NetworkModuleTests(unittest.TestCase):
    def test_ssh_prefers_the_last_working_port(self):
        checked_ports = []

        def fake_tcp_port_is_open(ip, port, timeout=networking.NETWORK_TIMEOUT_SECONDS):
            checked_ports.append(port)
            return port == 49151

        with patch.object(networking, 'tcp_port_is_open', side_effect=fake_tcp_port_is_open):
            port = networking.detect_ssh_port('192.168.3.73', preferred_port=49151)

        self.assertEqual(port, 49151)
        self.assertEqual(checked_ports, [49151])

    def test_ssh_port_cache_keeps_only_valid_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / 'network_cache.json'
            networking.save_ssh_port_cache(
                {'192.168.3.73': 49151, 'not-an-ip': 22, '192.168.3.9': 9999},
                str(cache_path),
            )
            self.assertEqual(
                networking.load_ssh_port_cache(str(cache_path)),
                {'192.168.3.73': 49151},
            )


class RdpModuleTests(unittest.TestCase):
    def test_normalize_local_and_explicit_rdp_usernames(self):
        self.assertEqual(normalize_rdp_username('opera'), '.\\opera')
        self.assertEqual(normalize_rdp_username('PC\\opera'), 'PC\\opera')
        self.assertEqual(normalize_rdp_username('persona@empresa.example'), 'persona@empresa.example')


class SettingsModuleTests(unittest.TestCase):
    def test_ui_settings_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / 'ui_settings.json'
            expected = {
                'geometry': '900x600+10+20',
                'sort_column': 'ip',
                'sort_ascending': False,
            }
            save_ui_settings(expected, str(settings_path))
            self.assertEqual(load_ui_settings(str(settings_path)), expected)


class UiModuleTests(unittest.TestCase):
    def test_format_status_text_includes_progress(self):
        self.assertEqual(
            format_status_text(122, 8, '12:34:56', 5, 366, False, True),
            '122 equipos · 8 visibles · hoja 12:34:56 · Comprobando red: 5/366',
        )
