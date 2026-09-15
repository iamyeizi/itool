import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from itool_app import network
from itool_app.rdp import rdp_username
from itool_app.ui import status_text


class NetworkModuleTests(unittest.TestCase):
    def test_ssh_prefers_the_last_working_port(self):
        checked_ports = []

        def fake_is_port_open(ip, port, timeout=network.NETWORK_TIMEOUT_SECONDS):
            checked_ports.append(port)
            return port == 49151

        with patch.object(network, 'is_port_open', side_effect=fake_is_port_open):
            port = network.get_open_ssh_port('192.168.3.73', preferred_port=49151)

        self.assertEqual(port, 49151)
        self.assertEqual(checked_ports, [49151])

    def test_ssh_port_cache_keeps_only_valid_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / 'network_cache.json'
            network.save_ssh_port_cache(
                {'192.168.3.73': 49151, 'not-an-ip': 22, '192.168.3.9': 9999},
                str(cache_path),
            )
            self.assertEqual(
                network.load_ssh_port_cache(str(cache_path)),
                {'192.168.3.73': 49151},
            )


class RdpModuleTests(unittest.TestCase):
    def test_local_and_explicit_rdp_usernames(self):
        self.assertEqual(rdp_username('opera'), '.\\opera')
        self.assertEqual(rdp_username('PC\\opera'), 'PC\\opera')
        self.assertEqual(rdp_username('persona@empresa.example'), 'persona@empresa.example')


class UiModuleTests(unittest.TestCase):
    def test_status_text_includes_progress(self):
        self.assertEqual(
            status_text(122, '12:34:56', 5, 366, True),
            '122 equipos · hoja 12:34:56 · Comprobando: 5/366',
        )
