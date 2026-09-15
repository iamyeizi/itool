import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def load_main_module():
    fake_sheet = types.SimpleNamespace(get_all_records=lambda: [])
    fake_gspread = types.ModuleType("gspread")
    fake_gspread.authorize = lambda credentials: types.SimpleNamespace(
        open=lambda sheet_name: types.SimpleNamespace(sheet1=fake_sheet)
    )

    fake_service_account = types.ModuleType("oauth2client.service_account")
    fake_service_account.ServiceAccountCredentials = types.SimpleNamespace(
        from_json_keyfile_name=lambda path, scope: object()
    )
    fake_oauth = types.ModuleType("oauth2client")
    fake_oauth.service_account = fake_service_account

    fake_ping = types.ModuleType("pythonping")
    fake_ping.ping = lambda *args, **kwargs: types.SimpleNamespace(success=lambda: False)

    module_path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("itool_main_for_tests", module_path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {
            "gspread": fake_gspread,
            "oauth2client": fake_oauth,
            "oauth2client.service_account": fake_service_account,
            "pythonping": fake_ping,
        },
    ):
        spec.loader.exec_module(module)
    return module


class MainHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = load_main_module()

    @classmethod
    def tearDownClass(cls):
        cls.main.NETWORK_EXECUTOR.shutdown(wait=False, cancel_futures=True)
        cls.main.DATA_EXECUTOR.shutdown(wait=False, cancel_futures=True)

    def test_rdp_username_uses_remote_local_account(self):
        self.assertEqual(self.main.rdp_username("opera"), ".\\opera")
        self.assertEqual(self.main.rdp_username("PC\\" + "opera"), "PC\\opera")
        self.assertEqual(
            self.main.rdp_username("persona@empresa.example"),
            "persona@empresa.example",
        )

    def test_ssh_prefers_the_last_working_port(self):
        checked_ports = []

        def fake_is_port_open(ip, port):
            checked_ports.append(port)
            return port == 49151

        with patch.object(self.main, "is_port_open", side_effect=fake_is_port_open):
            port = self.main.get_open_ssh_port("192.168.3.73", preferred_port=49151)

        self.assertEqual(port, 49151)
        self.assertEqual(checked_ports, [49151])

    def test_ssh_port_cache_keeps_only_valid_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "network_cache.json"
            with patch.object(self.main, "_network_cache_path", return_value=str(cache_path)):
                self.main.save_ssh_port_cache(
                    {"192.168.3.73": 49151, "not-an-ip": 22, "192.168.3.9": 9999}
                )
                self.assertEqual(
                    self.main.load_ssh_port_cache(),
                    {"192.168.3.73": 49151},
                )


if __name__ == "__main__":
    unittest.main()
