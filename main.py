import tempfile
import tkinter as tk
from tkinter import ttk
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from pythonping import ping
import threading
import subprocess
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import socket
import logging
import json
from queue import Empty, Queue
from functools import partial
import platform
import shutil
import shlex
import sys

# Configuración de logging
try:
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'itool.log')
except Exception:
    # Fallback al archivo en el cwd si no se puede crear la carpeta
    log_file = 'itool.log'

logging.basicConfig(
    filename=log_file,
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    filemode='a'
)

# También mostrar logs en consola
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
logging.getLogger().addHandler(console_handler)

# --- Helpers para rutas de recursos (compatible con PyInstaller) ---
def _resource_base_dir():
    try:
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            return sys._MEIPASS  # Carpeta temporal creada por PyInstaller
    except Exception:
        pass
    return os.path.dirname(os.path.abspath(__file__))

BASE_DIR = _resource_base_dir()

# --- Configuración Google Sheets ---
SCOPE = ['https://spreadsheets.google.com/feeds',
         'https://www.googleapis.com/auth/drive']
credential_path = os.path.join(BASE_DIR, 'credential.json')
CREDS = ServiceAccountCredentials.from_json_keyfile_name(credential_path, SCOPE)
gc = gspread.authorize(CREDS)
sheet = gc.open('bd_pcs').sheet1  # Cambia por el nombre de tu sheet

# --- Variables globales ---
SSH_PORTS = (22, 49151, 4402, 16166, 2222)
NETWORK_TIMEOUT_SECONDS = 0.5
NETWORK_CACHE_SECONDS = 30
NETWORK_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix='itool-net')
DATA_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix='itool-data')


def _network_cache_path():
    if platform.system().lower() == 'windows':
        state_dir = os.path.join(os.getenv('LOCALAPPDATA', os.path.expanduser('~')), 'iTool')
    else:
        state_dir = os.path.join(os.getenv('XDG_STATE_HOME', os.path.expanduser('~/.local/state')), 'itool')
    return os.path.join(state_dir, 'network_cache.json')


def load_ssh_port_cache():
    """Carga únicamente puertos SSH válidos previamente detectados."""
    try:
        with open(_network_cache_path(), encoding='utf-8') as cache_file:
            cached_ports = json.load(cache_file).get('ssh_ports', {})
        return {
            ip: port
            for ip, port in cached_ports.items()
            if is_valid_ip(ip) and port in SSH_PORTS
        }
    except (OSError, ValueError, TypeError):
        return {}


def save_ssh_port_cache(ssh_port_cache):
    """Guarda de forma atómica los puertos SSH que tuvieron éxito."""
    cache_path = _network_cache_path()
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
        logging.debug(f"No se pudo guardar el cache de puertos SSH: {error}")

# --- Optimización de la lectura de Google Sheets ---
def get_pc_list():
    try:
        logging.info("Obteniendo datos de Google Sheets...")
        data = sheet.get_all_records()
        logging.info(f"Datos obtenidos: {len(data)} registros")
        return data
    except Exception as e:
        logging.error(f"Error al leer Google Sheets: {e}")
        print(f"Error al leer Google Sheets: {e}")
        return []

# --- Validación de direcciones IP ---
def is_valid_ip(ip):
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


def rdp_username(usuario):
    """Devuelve un usuario explícitamente local cuando no se indicó ámbito.

    RDP interpreta ``EQUIPO\\usuario`` como una cuenta del equipo indicado.
    Las credenciales simples de la planilla son cuentas locales del destino, por
    lo que se convierten a ``.\\usuario``. Las cuentas de dominio y Microsoft
    se preservan tal como fueron cargadas.
    """
    usuario = str(usuario).strip()
    if "\\" in usuario or "@" in usuario:
        return usuario
    return f".\\{usuario}"

# --- Clave de ordenamiento natural para IPs (IPv4) ---
def ip_sort_key(value):
    """Devuelve una tupla numérica para ordenar IPs de forma natural.

    - IPs válidas: (0, oct1, oct2, oct3, oct4)
    - Vacías/Inválidas: (1, 0, 0, 0, 0)  -> van al final
    """
    ip_str = str(value or '').strip()
    parts = ip_str.split('.')
    if len(parts) == 4:
        try:
            octs = [int(p) for p in parts]
            if all(0 <= o <= 255 for o in octs):
                return (0, octs[0], octs[1], octs[2], octs[3])
        except Exception:
            pass
    return (1, 0, 0, 0, 0)

def ip_last_octet_sort_key(value):
    """Clave de ordenamiento por último octeto (host) para IPs IPv4.

    - IPs válidas: (0, last_octet)
    - Vacías/Inválidas: (1, 0) -> al final
    """
    ip_str = str(value or '').strip()
    parts = ip_str.split('.')
    if len(parts) == 4:
        try:
            last = int(parts[3])
            if 0 <= last <= 255:
                return (0, last)
        except Exception:
            pass
    return (1, 0)

def ip_vlan_host_sort_key(value):
    """Clave de ordenamiento por VLAN (3er octeto) y host (4to octeto).

    - IPs válidas: (0, vlan, host)
    - Vacías/Inválidas: (1, 0, 0) -> al final
    """
    ip_str = str(value or '').strip()
    parts = ip_str.split('.')
    if len(parts) == 4:
        try:
            vlan = int(parts[2])
            host = int(parts[3])
            if 0 <= vlan <= 255 and 0 <= host <= 255:
                return (0, vlan, host)
        except Exception:
            pass
    return (1, 0, 0)

# --- Ping asincrónico con manejo de PCs sin IP ---
def check_ping(ip):
    if not ip:
        return False

    if not is_valid_ip(ip):
        return False

    try:
        return ping(ip, 1, 1).success()
    except Exception as e:
        logging.debug(f"Error al hacer ping a {ip}: {e}")
        # Fallback en Linux sin privilegios para usar comando del sistema.
        if platform.system().lower() == 'linux':
            try:
                proc = subprocess.run(
                    ['ping', '-c', '1', '-W', '1', ip],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return proc.returncode == 0
            except Exception as fallback_error:
                logging.debug(f"Fallback ping fallo para {ip}: {fallback_error}")
        return False

def is_port_open(ip, port):
    """Verifica si un puerto específico está abierto en una IP dada."""
    if not ip or not is_valid_ip(ip):
        return False

    def check_port():
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(NETWORK_TIMEOUT_SECONDS)
                result = s.connect_ex((ip, port))
                return result == 0
        except Exception:
            return False

    try:
        return check_port()
    except Exception as e:
        logging.debug(f"Error al verificar el puerto {port} en {ip}: {e}")
        return False


def get_open_ssh_port(ip, preferred_port=None):
    """Devuelve un puerto SSH, probando primero el último que funcionó."""
    if not ip or not is_valid_ip(ip):
        return None

    ports = ((preferred_port,) if preferred_port in SSH_PORTS else ())
    ports += tuple(port for port in SSH_PORTS if port != preferred_port)
    return next((port for port in ports if is_port_open(ip, port)), None)

class iToolApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("iTool")
        # Plataforma
        self.system = platform.system().lower()  # 'windows', 'linux', 'darwin'
        # Windows: fijar AppUserModelID para que la barra de tareas agrupe/identifique correctamente
        self._set_windows_app_id()
        # Icono de la aplicación (utils/app.ico para Windows, utils/app.png para Linux/macOS)
        self._set_app_icon()
        # Estructuras de datos
        self.pc_list = []
        self.filtered_list = []
        self.leds = []
        self.ssh_buttons = []
        self.rdp_buttons = []  # Para trackear botones RDP
        self.filter_timer = None   # Para debounce del filtro
        self.sort_column = None    # Columna actual de ordenamiento
        self.sort_ascending = True # Dirección del ordenamiento
        self.window_size_set = False  # Flag para evitar múltiples ajustes de ventana
        self.search_index = {}
        self.visible_row_ids = ()
        self.last_search_query = None
        self.ui_events = Queue()
        self.pending_checks = set()
        self.data_loading = False

        # Cache para resultados de ping y puertos
        self.ping_cache = {}       # IP -> bool (ping result)
        self.ssh_port_cache = load_ssh_port_cache()
        self.rdp_port_cache = {}   # IP -> bool (port 3389)
        self.cache_timeout = NETWORK_CACHE_SECONDS
        self.last_check_time = {}  # (tipo, IP) -> timestamp
        self.ssh_cache_dirty = False
        self.ssh_cache_save_timer = None

        # Hacer que la ventana no sea redimensionable
        self.resizable(False, False)

        # Inicializar interfaz y datos
        self.create_widgets()
        self.after(50, self._drain_ui_events)
        self.after(1, self.refresh_data)
        self.after(self.cache_timeout * 1000, self._periodic_network_refresh)

    def _set_app_icon(self):
        """Configura el icono de la ventana según el sistema operativo.

        - Windows: intenta utils/app.ico (iconbitmap). Fallback: utils/app.png via iconphoto.
        - Linux/macOS: intenta utils/app.png via iconphoto. Fallback: utils/app.ico via iconphoto.
        No falla si no encuentra archivos; solo loggea un warning.
        """
        try:
            utils_dir = os.path.join(BASE_DIR, 'utils')
            ico_candidates = [
                os.path.join(utils_dir, 'app.ico'),
                os.path.join(utils_dir, 'icon.ico'),
            ]
            png_candidates = [
                os.path.join(utils_dir, 'app.png'),
                os.path.join(utils_dir, 'icon.png'),
            ]

            if self.system == 'windows':
                # En Windows, preferir .ico para iconbitmap (ícono de ventana y taskbar en exe empaquetado)
                for p in ico_candidates:
                    if os.path.exists(p):
                        try:
                            self.iconbitmap(p)
                            return
                        except Exception as e:
                            logging.debug(f"iconbitmap con ICO falló: {e}")
                for p in png_candidates:
                    if os.path.exists(p):
                        try:
                            self.iconphoto(True, tk.PhotoImage(file=p))
                            return
                        except Exception as e:
                            logging.debug(f"iconphoto con PNG falló: {e}")
            else:
                # En Linux/macOS, preferir PNG pero hacer fallback a ICO si es lo único
                for p in png_candidates:
                    if os.path.exists(p):
                        try:
                            self.iconphoto(True, tk.PhotoImage(file=p))
                            return
                        except Exception as e:
                            logging.debug(f"iconphoto con PNG falló: {e}")
                for p in ico_candidates:
                    if os.path.exists(p):
                        try:
                            self.iconphoto(True, tk.PhotoImage(file=p))
                            return
                        except Exception as e:
                            logging.debug(f"iconphoto con ICO falló: {e}")

            logging.warning("Icono de app no encontrado. Ubicá utils/app.ico (Windows) o utils/app.png (Linux/macOS). También se aceptan utils/icon.ico y utils/icon.png.")
        except Exception as e:
            logging.debug(f"No se pudo establecer icono: {e}")

    def _set_windows_app_id(self):
        """Establece el AppUserModelID en Windows para mejorar el ícono y el agrupado en la taskbar.

        Nota: esto no cambia el ícono de la taskbar si se ejecuta como .py con python.exe; para que la taskbar
        muestre tu ícono personalizado, empaquetá a .exe con tu ícono o usá un acceso directo con ícono.
        """
        try:
            if self.system == 'windows':
                import ctypes
                app_id = u"com.itool.app"
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        except Exception as e:
            logging.debug(f"No se pudo fijar AppUserModelID: {e}")

    def create_widgets(self):
        # Frame principal para organizar la interfaz
        main_frame = tk.Frame(self)
        main_frame.pack(fill='both', expand=True, padx=10, pady=5)

        # Buscador y botones arriba (FIJO)
        search_frame = tk.Frame(main_frame)
        search_frame.pack(fill='x', pady=(0, 5), expand=False)
        self.search_var = tk.StringVar()
        tk.Label(search_frame, text="Filtrar:").pack(side='left')
        search_entry = tk.Entry(search_frame, textvariable=self.search_var)
        search_entry.pack(side='left', fill='x', expand=True, padx=5)
        # Usar debounce para el filtro - solo filtrar después de 500ms sin escribir
        search_entry.bind('<KeyRelease>', self.on_search_change)
        search_entry.bind('<Return>', lambda e: self.apply_filter())
        search_entry.bind('<Escape>', lambda e: self.clear_filter())
        tk.Button(search_frame, text="🗑", command=self.clear_filter).pack(side='left', padx=2)
        tk.Button(search_frame, text="🔄", command=self.refresh_data).pack(side='left', padx=2)

        # Frame para headers (FIJO)
        self.headers_frame = tk.Frame(main_frame, bg='lightgray')
        self.headers_frame.pack(fill='x', pady=(0, 2), expand=False)

        # Canvas con scroll para el grid (SCROLLEABLE)
        container = tk.Frame(main_frame)
        container.pack(fill='both', expand=True)
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(container, borderwidth=0)
        self.scrollbar = tk.Scrollbar(
            container, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = tk.Frame(self.canvas)

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(
                scrollregion=self.canvas.bbox("all")
            )
        )

        self.canvas.create_window(
            (0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)

        # Bind para scroll con rueda del mouse
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)

        # Crear headers fijos
        self.create_fixed_headers()

    def _is_cache_valid(self, check_type, ip):
        """Verifica la vigencia de un resultado de red específico."""
        import time
        timestamp = self.last_check_time.get((check_type, ip))
        return timestamp is not None and time.time() - timestamp < self.cache_timeout

    def _submit_check(self, check_type, ip, worker):
        key = (check_type, ip)
        if key in self.pending_checks or self._is_cache_valid(check_type, ip):
            return

        self.pending_checks.add(key)
        future = NETWORK_EXECUTOR.submit(worker, ip)

        def publish_result(completed_future):
            try:
                result = completed_future.result()
            except Exception as error:
                logging.debug(f"Fallo la comprobación {check_type} para {ip}: {error}")
                result = None if check_type == 'ssh' else False
            self.ui_events.put(('network', check_type, ip, result))

        future.add_done_callback(publish_result)

    def _schedule_network_checks(self):
        """Encola chequeos limitados sin bloquear el hilo de la interfaz."""
        for ip in {str(pc.get('ip', '')).strip() for pc in self.pc_list}:
            if not is_valid_ip(ip):
                continue
            self._submit_check('ping', ip, check_ping)
            self._submit_check('rdp', ip, lambda target: is_port_open(target, 3389))
            preferred_port = self.ssh_port_cache.get(ip)
            self._submit_check(
                'ssh',
                ip,
                lambda target, preferred=preferred_port: get_open_ssh_port(target, preferred),
            )

    def _periodic_network_refresh(self):
        self._schedule_network_checks()
        self.after(self.cache_timeout * 1000, self._periodic_network_refresh)

    def _drain_ui_events(self):
        """Aplica resultados en el hilo de Tkinter, nunca desde un worker."""
        try:
            while True:
                event = self.ui_events.get_nowait()
                if event[0] == 'data':
                    self._apply_data(event[1])
                else:
                    _, check_type, ip, result = event
                    self._apply_network_result(check_type, ip, result)
        except Empty:
            pass
        self.after(50, self._drain_ui_events)

    def _apply_data(self, data):
        self.data_loading = False
        self.pc_list = data
        self.filtered_list = data.copy()
        self.search_index = {
            id(pc): f"{pc.get('ip', '')} {pc.get('titular', '')}".casefold()
            for pc in data
        }
        self.visible_row_ids = ()
        self.last_search_query = None
        logging.debug(f"Datos cargados: {len(self.pc_list)} PCs")
        self.create_grid()
        if not self.window_size_set:
            self.adjust_window_to_content()
            self.window_size_set = True
        self._schedule_network_checks()

    def _apply_network_result(self, check_type, ip, result):
        import time
        self.pending_checks.discard((check_type, ip))
        self.last_check_time[(check_type, ip)] = time.time()
        if check_type == 'ping':
            self.ping_cache[ip] = bool(result)
        elif check_type == 'rdp':
            self.rdp_port_cache[ip] = bool(result)
        else:
            previous_port = self.ssh_port_cache.get(ip)
            self.ssh_port_cache[ip] = result
            if result and result != previous_port:
                self.ssh_cache_dirty = True
                if self.ssh_cache_save_timer is None:
                    self.ssh_cache_save_timer = self.after(1000, self._save_ssh_port_cache)
        self._update_widgets_for_ip(check_type, ip, result)

    def _save_ssh_port_cache(self):
        self.ssh_cache_save_timer = None
        if self.ssh_cache_dirty:
            save_ssh_port_cache(self.ssh_port_cache)
            self.ssh_cache_dirty = False

    def _update_widgets_for_ip(self, check_type, ip, result):
        if check_type == 'ping':
            for led, led_ip in self.leds:
                if led_ip == ip and led.winfo_exists():
                    led.config(fg='green' if result else 'red')
        elif check_type == 'rdp':
            for button, button_ip in self.rdp_buttons:
                if button_ip == ip and button.winfo_exists():
                    button.config(
                        state='normal' if result else 'disabled',
                        text='RDP' if result else '✗',
                    )
        else:
            for button, button_ip in self.ssh_buttons:
                if button_ip == ip and button.winfo_exists():
                    button.config(
                        state='normal' if result else 'disabled',
                        text='SSH' if result else '✗',
                    )

    def _apply_cached_network_statuses(self):
        for ip, status in self.ping_cache.items():
            self._update_widgets_for_ip('ping', ip, status)
        for ip, status in self.rdp_port_cache.items():
            self._update_widgets_for_ip('rdp', ip, status)
        for ip, port in self.ssh_port_cache.items():
            self._update_widgets_for_ip('ssh', ip, port)

    def create_fixed_headers(self):
        """Crea los headers fijos que no se mueven al hacer scroll"""
        # Limpiar headers existentes
        for widget in self.headers_frame.winfo_children():
            widget.destroy()
        headers = ["Titular", "IP", "Ping", "RDP", "SSH"]
        header_keys = ["titular", "ip", "", "", ""]  # Keys para ordenamiento

        for col, (h, key) in enumerate(zip(headers, header_keys)):
            if key:  # Solo las columnas con datos son clickeables
                text = h
                if self.sort_column == key:
                    text += " ↓" if self.sort_ascending else " ↑"
                header_label = tk.Label(
                    self.headers_frame,
                    text=text,
                    font=("Arial", 10, "bold"),
                    bg='lightblue' if self.sort_column == key else 'lightgray',
                    relief='raised', bd=1, cursor="hand2", anchor='w'
                )
                header_label.bind("<Button-1>", lambda e, column=key: self.sort_by_column(column))
            else:
                header_label = tk.Label(
                    self.headers_frame,
                    text=h,
                    font=("Arial", 10, "bold"),
                    bg='lightgray', relief='raised', bd=1, anchor='w'
                )
            header_label.grid(row=0, column=col, padx=2, pady=1, sticky="nsew")

        self.headers_frame.grid_rowconfigure(0, weight=1)

    def sort_by_column(self, column):
        """Ordena la lista por la columna especificada"""
        logging.info(f"Ordenando por columna: {column}")

        # Si ya estamos ordenando por esta columna, cambiar dirección
        if self.sort_column == column:
            self.sort_ascending = not self.sort_ascending
        else:
            self.sort_column = column
            self.sort_ascending = True

        # Ordenar la lista filtrada
        try:
            if column == 'ip':
                # Ordenar primero por VLAN (3er octeto) y luego por host (4to octeto)
                self.filtered_list.sort(
                    key=lambda x: ip_vlan_host_sort_key(x.get('ip', '')),
                    reverse=not self.sort_ascending
                )
            else:
                self.filtered_list.sort(
                    key=lambda x: str(x.get(column, '')).lower(),
                    reverse=not self.sort_ascending
                )
            logging.debug(f"Lista ordenada por {column}, ascendente: {self.sort_ascending}")

            # Actualizar headers para mostrar el indicador de ordenamiento
            self.create_fixed_headers()

            # Actualizar la visualización
            # Optimización: durante ordenamiento evitamos lanzar comprobaciones de red
            self.update_grid_display(from_sort=True)

        except Exception as e:
            logging.error(f"Error al ordenar por {column}: {e}")

    def _on_mousewheel(self, event):
        """Permite scroll con la rueda del mouse"""
        self.canvas.yview_scroll(int(-1*(event.delta/120)), "units")

    def on_search_change(self, event):
        """Filtra con una espera breve sin reconstruir la UI innecesariamente."""
        if self.filter_timer:
            self.after_cancel(self.filter_timer)
        self.filter_timer = self.after(150, self.apply_filter)

    def apply_filter(self):
        """Aplica el filtro usando el índice precalculado de cada PC."""
        self.filter_timer = None
        query = self.search_var.get().casefold().strip()
        if query == self.last_search_query:
            return

        self.last_search_query = query
        logging.info(f"Aplicando filtro: '{query}'")
        if not query:
            filtered_list = self.pc_list.copy()
        else:
            filtered_list = [
                pc for pc in self.pc_list
                if query in self.search_index.get(id(pc), '')
            ]

        visible_row_ids = tuple(id(pc) for pc in filtered_list)
        self.filtered_list = filtered_list
        if visible_row_ids == self.visible_row_ids:
            return

        logging.debug(f"Resultados del filtro: {len(filtered_list)} PCs")
        self.update_grid_display()

    def refresh_data(self):
        if self.data_loading:
            return

        logging.info("Refrescando datos desde Google Sheets")
        self.data_loading = True
        future = DATA_EXECUTOR.submit(get_pc_list)

        def publish_data(completed_future):
            try:
                data = completed_future.result()
            except Exception as error:
                logging.error(f"Error al refrescar datos: {error}")
                data = []
            self.ui_events.put(('data', data))

        future.add_done_callback(publish_data)

    def clear_filter(self):
        logging.info("Limpiando filtro")
        self.search_var.set("")
        self.last_search_query = None
        self.apply_filter()

    def adjust_window_to_content(self):
        """Ajusta la ventana al contenido - ancho fijo basado en contenido, alto para máximo 20 filas"""
        self.update_idletasks()

        # Calcular el ancho necesario basado en el contenido más largo de cada columna
        column_widths = self.calculate_column_widths()
        total_width = sum(column_widths) + 60  # +60 para márgenes, padding y scrollbar

        # Altura fija para siempre 20 filas
        row_height = 30  # Altura por fila
        base_height = 120  # Para filtro, headers y márgenes
        content_height = 20 * row_height  # SIEMPRE 20 filas
        total_height = base_height + content_height

        # Centrar la ventana en la pantalla
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        x = (screen_width - total_width) // 2
        y = (screen_height - total_height) // 2

        # Configurar el tamaño mínimo y máximo para evitar redimensionamiento en ancho
        self.minsize(total_width, total_height)
        self.maxsize(total_width, total_height)  # Fijar también la altura

        self.geometry(f"{total_width}x{total_height}+{x}+{y}")
        logging.debug(f"Ventana ajustada a: {total_width}x{total_height} en posición {x},{y}")

    def calculate_column_widths(self):
        """Calcula el ancho óptimo para cada columna basado en su contenido"""
        headers = ["Titular", "IP", "Ping", "RDP", "SSH"]
        column_widths = []

        for col, header in enumerate(headers):
            max_length = len(header)  # Empezar con la longitud del header

            # Buscar el contenido más largo en cada columna usando TODA la lista, no solo filtrada
            if col == 0:  # Titular
                for pc in self.pc_list:  # Usar pc_list completa en lugar de filtered_list
                    max_length = max(max_length, len(str(pc.get('titular', ''))))
            elif col == 1:  # IP
                for pc in self.pc_list:
                    max_length = max(max_length, len(str(pc.get('ip', ''))))
            elif col == 2:  # Ping (solo el LED)
                max_length = 4  # Ancho fijo para el LED
            elif col in [3, 4]:  # Botones
                max_length = max(max_length, 8)  # Ancho mínimo para botones

            # Convertir caracteres a píxeles (aproximado: 1 carácter = 8 píxeles)
            # Reducir el padding para evitar espacio extra
            width_pixels = max_length * 8 + 15  # +15 para padding
            column_widths.append(width_pixels)

        return column_widths

    def sync_column_widths(self):
        """Sincroniza el ancho de las columnas entre headers y contenido basado en contenido"""
        try:
            self.update_idletasks()

            # Calcular anchos óptimos basados en contenido
            column_widths = self.calculate_column_widths()

            # Aplicar el ancho calculado a todas las columnas
            for col, width in enumerate(column_widths):
                self.headers_frame.grid_columnconfigure(col, minsize=width, weight=0)
                self.scrollable_frame.grid_columnconfigure(col, minsize=width, weight=0)

        except Exception as e:
            logging.debug(f"Error al sincronizar anchos de columna: {e}")

    def update_grid_display(self, from_sort: bool = False):
        """Actualiza la visualización del grid alineada con los headers"""
        logging.info("Actualizando visualización del grid")
        self.visible_row_ids = tuple(id(pc) for pc in self.filtered_list)
        # Limpiar todas las filas existentes
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()
        # Reiniciar seguimiento
        self.leds.clear()
        self.ssh_buttons.clear()
        self.rdp_buttons.clear()

        # Crear cada celda directamente en scrollable_frame para alinear columnas
        for row, pc in enumerate(self.filtered_list):
            # Titular
            tk.Label(self.scrollable_frame, text=pc.get('titular', ''), anchor='w',
                    bg='white' if row % 2 == 0 else '#f0f0f0').grid(row=row, column=0, padx=2, sticky='nsew')
            # IP
            tk.Label(self.scrollable_frame, text=pc.get('ip', ''), anchor='w',
                    bg='white' if row % 2 == 0 else '#f0f0f0').grid(row=row, column=1, padx=2, sticky='nsew')
            # LED Ping
            led = tk.Label(self.scrollable_frame, text='●', fg='grey', font=('Arial', 12),
                          bg='white' if row % 2 == 0 else '#f0f0f0')
            led.grid(row=row, column=2, padx=2, sticky='nsew')
            self.leds.append((led, pc.get('ip', '')))
            # Botón RDP
            btn_normal = tk.Button(self.scrollable_frame, text='RDP', state='disabled',
                                   command=partial(self.connect_login_remoto, pc))
            btn_normal.grid(row=row, column=3, padx=2, sticky='nsew')
            self.rdp_buttons.append((btn_normal, pc.get('ip', '')))  # Trackear para verificar puerto
            if self.system != 'windows' and not self._get_linux_rdp_client():
                btn_normal.config(state='disabled', text='N/A')
            # Botón SSH
            btn_ssh = tk.Button(self.scrollable_frame, text='✗', state='disabled',
                                 command=partial(self.connect_ssh, pc))
            btn_ssh.grid(row=row, column=4, padx=2, sticky='nsew')
            self.ssh_buttons.append((btn_ssh, pc.get('ip', '')))

            # Configurar el peso de cada fila
            self.scrollable_frame.grid_rowconfigure(row, weight=1)

        self._apply_cached_network_statuses()
        self.after(100, self.sync_column_widths)

    def create_grid(self):
        """Inicializa el grid básico"""
        logging.info("Inicializando grid de PCs")

        # Limpiar cualquier widget existente
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()

        self.leds.clear()
        self.ssh_buttons.clear()
        self.rdp_buttons.clear()

        # Actualizar headers fijos
        self.create_fixed_headers()

        # Actualizar la visualización con las PCs
        self.update_grid_display()

    def connect_login_remoto(self, pc):
        """Conecta usando credenciales del PC"""
        if not pc.get('ip', '') or not pc.get('usuario', '') or not pc.get('contrasenia', ''):
            logging.warning("Datos incompletos para conexión normal")
            return
        ip = pc["ip"]
        if self.system == 'windows':
            logging.info(f"Conectando normalmente a {ip} (Windows)")
            usuario_rdp = rdp_username(pc["usuario"])
            # Guarda las credenciales en el Administrador de Credenciales de Windows
            try:
                subprocess.call([
                    'cmdkey',
                    f'/add:TERMSRV/{ip}',
                    f'/user:{usuario_rdp}',
                    f'/pass:{pc["contrasenia"]}'
                ])
            except FileNotFoundError:
                logging.error("cmdkey no encontrado")

            try:
                template_path = os.path.join(BASE_DIR, 'utils', 'template.rdp')
                lines = None
                for enc in ('utf-16', 'utf-8-sig', None):
                    try:
                        if enc is None:
                            with open(template_path, 'r') as f:
                                lines = f.readlines()
                        else:
                            with open(template_path, 'r', encoding=enc) as f:
                                lines = f.readlines()
                        break
                    except Exception:
                        continue
                if lines is None:
                    raise FileNotFoundError('No se pudo leer utils/template.rdp con las codificaciones esperadas')
            except FileNotFoundError:
                logging.error("Archivo utils/template.rdp no encontrado")
                return

            new_lines = []
            username_set = False
            for line in lines:
                if line.strip().startswith('full address:s:'):
                    new_lines.append(f'full address:s:{ip}\r\n')
                elif line.strip().startswith('username:s:'):
                    new_lines.append(f'username:s:{usuario_rdp}\r\n')
                    username_set = True
                elif line.strip().startswith('prompt for credentials:i:'):
                    new_lines.append('prompt for credentials:i:0\r\n')
                elif line.strip().startswith('promptcredentialonce:i:'):
                    new_lines.append('promptcredentialonce:i:1\r\n')
                else:
                    new_lines.append(line)

            if not username_set:
                new_lines.append(f'username:s:{usuario_rdp}\r\n')
                new_lines.append('prompt for credentials:i:0\r\n')
                new_lines.append('promptcredentialonce:i:1\r\n')

            temp_rdp = f'rdp_{ip.replace(".", "_")}.rdp'
            with open(temp_rdp, 'w', encoding='utf-16') as f:
                f.writelines(new_lines)

            try:
                subprocess.Popen(['mstsc', temp_rdp])
            except FileNotFoundError:
                logging.error("mstsc no encontrado para conexión normal")
            threading.Timer(10, lambda: os.remove(temp_rdp) if os.path.exists(temp_rdp) else None).start()
            # Borrar credenciales
            threading.Timer(60, lambda: subprocess.call([
                'cmdkey', f'/delete:TERMSRV/{ip}'
            ])).start()
        else:
            # Linux / otros
            rdp_client = self._get_linux_rdp_client()
            if not rdp_client:
                logging.warning("No se encontró cliente RDP (instala xfreerdp o remmina)")
                return
            logging.info(f"Conectando a {ip} con {rdp_client} (Linux)")
            if 'xfreerdp' in rdp_client:
                comando = [rdp_client, f"/v:{ip}", f"/u:{pc['usuario']}", f"/p:{pc['contrasenia']}", '/cert:ignore']
            elif 'remmina' in rdp_client:
                # Remmina no acepta user/pass directamente en CLI simple, se usa URL
                comando = [rdp_client, f"--conn=rdp://{pc['usuario']}:{pc['contrasenia']}@{ip}"]
            else:
                comando = [rdp_client, ip]
            try:
                subprocess.Popen(comando)
            except Exception as e:
                logging.error(f"Error iniciando cliente RDP Linux: {e}")

    def connect_ssh(self, pc):
        if not pc or not pc.get('ip', ''):
            return

        ip = pc.get('ip', '')
        usuario = pc.get('usuario', '')
        contrasenia = pc.get('contrasenia', '')

        # Usar el puerto detectado; 22 es el fallback antes de una comprobación.
        current_ssh_port = self.ssh_port_cache.get(ip) or SSH_PORTS[0]

        if self.system == 'windows':
            unique_id = uuid.uuid4().hex[:8]
            temp_dir = tempfile.gettempdir()
            bat_filename = os.path.join(temp_dir, f"connect_ssh_{unique_id}.bat")
            bat_content = f"""@echo off
chcp 65001 > nul
echo.
echo CONTRASEÑA: {contrasenia}
echo.
ssh {usuario}@{ip} -p {current_ssh_port}
echo.
pause
del "%~f0"
"""
            with open(bat_filename, "w", encoding="utf-8") as f:
                f.write(bat_content)
            comando = ["cmd.exe", "/c", f"start cmd /k {bat_filename}"]
            try:
                subprocess.Popen(comando)
            except FileNotFoundError:
                logging.error("cmd.exe no disponible")
        else:
            # Linux: abrir nueva terminal
            # Mostrar contraseña igual que en Windows antes de ejecutar ssh
            safe_user = shlex.quote(usuario)
            safe_host = shlex.quote(ip)
            ssh_cmd = f"ssh {safe_user}@{safe_host} -p {current_ssh_port}"
            show_pass = f"echo; echo 'CONTRASEÑA: {shlex.quote(contrasenia)}'; echo;"
            term_emulators = [
                ('gnome-terminal', ['gnome-terminal', '--', 'bash', '-c', f"{show_pass}{ssh_cmd}; exec bash"]),
                ('konsole', ['konsole', '-e', f"bash -c \"{show_pass}{ssh_cmd}; exec bash\""]),
                ('x-terminal-emulator', ['x-terminal-emulator', '-e', f"bash -c \"{show_pass}{ssh_cmd}; exec bash\""]),
                ('xterm', ['xterm', '-e', f"bash -c \"{show_pass}{ssh_cmd}; bash\""]),
            ]
            launched = False
            for name, cmd in term_emulators:
                if shutil.which(name):
                    try:
                        subprocess.Popen(cmd)
                        launched = True
                        break
                    except Exception as e:
                        logging.debug(f"Fallo lanzando {name}: {e}")
            if not launched:
                logging.warning("No se encontró un emulador de terminal compatible; no se puede mostrar la contraseña antes de ssh. Instalá gnome-terminal, konsole o xterm.")
                try:
                    subprocess.Popen(['ssh', f'{usuario}@{ip}', '-p', str(current_ssh_port)])
                except Exception as e:
                    logging.error(f"No se pudo lanzar SSH: {e}")

    # ---------------- Utilidades específicas de plataforma ---------------- #
    def _get_linux_rdp_client(self):
        """Devuelve el primer cliente RDP disponible en Linux"""
        if self.system == 'windows':
            return 'mstsc'
        for candidate in ['xfreerdp', 'remmina']:  # orden preferencia
            if shutil.which(candidate):
                return candidate
        return None

if __name__ == "__main__":
    logging.info("Iniciando iTool")
    app = iToolApp()
    app.mainloop()
