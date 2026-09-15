import ipaddress
import json
import logging
import os
import platform
import socket
import subprocess

from pythonping import ping


SSH_PORTS = (22, 49151, 4402, 16166, 2222)
NETWORK_TIMEOUT_SECONDS = 0.5


def is_valid_ip(ip):
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


def ip_vlan_host_sort_key(value):
    ip_str = str(value or '').strip()
    parts = ip_str.split('.')
    if len(parts) == 4:
        try:
            octets = [int(part) for part in parts]
            if all(0 <= octet <= 255 for octet in octets):
                return (0, octets[2], octets[3])
        except ValueError:
            pass
    return (1, 0, 0)


def check_ping(ip):
    if not is_valid_ip(ip):
        return False
    try:
        return ping(ip, 1, 1).success()
    except Exception as error:
        logging.debug("Error al hacer ping a %s: %s", ip, error)
        if platform.system().lower() == 'linux':
            try:
                return subprocess.run(
                    ['ping', '-c', '1', '-W', '1', ip],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ).returncode == 0
            except OSError:
                return False
        return False


def is_port_open(ip, port, timeout=NETWORK_TIMEOUT_SECONDS):
    if not is_valid_ip(ip):
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            return connection.connect_ex((ip, port)) == 0
    except OSError:
        return False


def get_open_ssh_port(ip, preferred_port=None):
    if not is_valid_ip(ip):
        return None
    ports = ((preferred_port,) if preferred_port in SSH_PORTS else ())
    ports += tuple(port for port in SSH_PORTS if port != preferred_port)
    return next((port for port in ports if is_port_open(ip, port)), None)


def network_cache_path():
    if platform.system().lower() == 'windows':
        state_dir = os.path.join(os.getenv('LOCALAPPDATA', os.path.expanduser('~')), 'iTool')
    else:
        state_dir = os.path.join(os.getenv('XDG_STATE_HOME', os.path.expanduser('~/.local/state')), 'itool')
    return os.path.join(state_dir, 'network_cache.json')


def load_ssh_port_cache(cache_path=None):
    try:
        with open(cache_path or network_cache_path(), encoding='utf-8') as cache_file:
            cached_ports = json.load(cache_file).get('ssh_ports', {})
        return {
            ip: port
            for ip, port in cached_ports.items()
            if is_valid_ip(ip) and port in SSH_PORTS
        }
    except (OSError, ValueError, TypeError):
        return {}


def save_ssh_port_cache(ssh_port_cache, cache_path=None):
    cache_path = cache_path or network_cache_path()
    state_dir = os.path.dirname(cache_path)
    valid_ports = {
        ip: port
        for ip, port in ssh_port_cache.items()
        if is_valid_ip(ip) and port in SSH_PORTS
    }
    try:
        os.makedirs(state_dir, exist_ok=True)
        temp_path = f'{cache_path}.tmp'
        with open(temp_path, 'w', encoding='utf-8') as cache_file:
            json.dump({'ssh_ports': valid_ports}, cache_file, sort_keys=True)
        os.replace(temp_path, cache_path)
    except OSError as error:
        logging.debug("No se pudo guardar el cache de puertos SSH: %s", error)
