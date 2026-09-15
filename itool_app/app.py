import tempfile
import tkinter as tk
import threading
import subprocess
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
import logging
from queue import Empty, Queue
from functools import partial
import platform
import shutil
import shlex
import sys

from itool_app.networking import (
    SSH_PORTS,
    detect_ssh_port,
    ip_vlan_host_sort_key,
    ping_host,
    tcp_port_is_open,
    is_valid_ip,
    load_ssh_port_cache,
    save_ssh_port_cache,
)
from itool_app.remote_desktop import normalize_rdp_username
from itool_app.sheets_source import fetch_pc_records
from itool_app.settings import load_ui_settings, save_ui_settings
from itool_app.ui_components import format_status_text

# Configuración de logging
try:
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
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
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BASE_DIR = _resource_base_dir()

# --- Variables globales ---
NETWORK_CACHE_SECONDS = 30
NETWORK_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix='itool-net')
DATA_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix='itool-data')

def fetch_pc_list():
    try:
        logging.info("Obteniendo datos de Google Sheets...")
        data = fetch_pc_records(BASE_DIR)
        logging.info(f"Datos obtenidos: {len(data)} registros")
        return data
    except Exception as e:
        logging.error(f"Error al leer Google Sheets: {e}")
        print(f"Error al leer Google Sheets: {e}")
        return []

class IToolApp(tk.Tk):
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
        self.ui_settings = load_ui_settings()
        saved_column = self.ui_settings.get('sort_column')
        self.sort_column = saved_column if saved_column in ('titular', 'ip') else None
        self.sort_ascending = bool(self.ui_settings.get('sort_ascending', True))
        self.window_size_set = False  # Flag para evitar múltiples ajustes de ventana
        self.search_index = {}
        self.visible_row_ids = ()
        self.last_search_query = None
        self.ui_events = Queue()
        self.pending_checks = set()
        self.active_check_keys = set()
        self.data_loading = False
        self.last_sheet_update = None
        self.spinner_index = 0

        # Cache para resultados de ping y puertos
        self.ping_cache = {}       # IP -> bool (ping result)
        self.ssh_port_cache = load_ssh_port_cache()
        self.rdp_port_cache = {}   # IP -> bool (port 3389)
        self.cache_timeout = NETWORK_CACHE_SECONDS
        self.last_check_time = {}  # (tipo, IP) -> timestamp
        self.ssh_cache_dirty = False
        self.ssh_cache_save_timer = None

        self.resizable(True, True)
        self.minsize(620, 360)
        self.protocol('WM_DELETE_WINDOW', self._save_ui_settings_and_close)

        # Inicializar interfaz y datos
        self.create_widgets()
        self.after(50, self._drain_ui_events)
        self.after(1, self.refresh_data)
        self.after(150, self._animate_status)
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
        self.refresh_button = tk.Button(search_frame, text="🔄", command=self.refresh_data)
        self.refresh_button.pack(side='left', padx=2)

        status_frame = tk.Frame(main_frame)
        status_frame.pack(fill='x', pady=(0, 4), expand=False)
        self.status_var = tk.StringVar(value='Cargando hoja…')
        self.spinner_var = tk.StringVar(value='○')
        tk.Label(status_frame, textvariable=self.spinner_var, width=2, anchor='w').pack(side='left')
        tk.Label(status_frame, textvariable=self.status_var, anchor='w').pack(side='left')

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

        self.canvas_window = self.canvas.create_window(
            (0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.bind('<Configure>', self._resize_grid_to_canvas)

        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)

        # Bind para scroll con rueda del mouse
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)

        # Crear headers fijos
        self.create_fixed_headers()

    def _resize_grid_to_canvas(self, event):
        self.canvas.itemconfigure(self.canvas_window, width=event.width)

    def _save_ui_settings_and_close(self):
        save_ui_settings({
            'geometry': self.geometry(),
            'sort_column': self.sort_column,
            'sort_ascending': self.sort_ascending,
        })
        self.destroy()

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
        ips = {str(pc.get('ip', '')).strip() for pc in self.pc_list}
        valid_ips = {ip for ip in ips if is_valid_ip(ip)}
        self.active_check_keys = {
            (check_type, ip)
            for ip in valid_ips
            for check_type in ('ping', 'rdp', 'ssh')
        }
        for ip in valid_ips:
            if not is_valid_ip(ip):
                continue
            self._submit_check('ping', ip, ping_host)
            self._submit_check('rdp', ip, lambda target: tcp_port_is_open(target, 3389))
            preferred_port = self.ssh_port_cache.get(ip)
            self._submit_check(
                'ssh',
                ip,
                lambda target, preferred=preferred_port: detect_ssh_port(target, preferred),
            )
        self._refresh_status()

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

    def _refresh_status(self):
        total_checks = len(self.active_check_keys)
        completed_checks = sum(
            1
            for key in self.active_check_keys
            if key not in self.pending_checks and self._is_cache_valid(*key)
        )
        network_loading = bool(self.pending_checks)
        self.status_var.set(
            format_status_text(
                len(self.pc_list),
                len(self.filtered_list),
                self.last_sheet_update,
                completed_checks,
                total_checks,
                self.data_loading,
                network_loading,
            )
        )
        self.spinner_var.set(
            '◌◓◑◒'[self.spinner_index % 4]
            if self.data_loading or network_loading else '●'
        )

    def _animate_status(self):
        self.spinner_index += 1
        self._refresh_status()
        self.after(150, self._animate_status)

    def _apply_data(self, data):
        self.data_loading = False
        self.refresh_button.config(state='normal')
        import time
        self.last_sheet_update = time.strftime('%H:%M:%S')
        self.pc_list = data
        self.search_index = {
            id(pc): f"{pc.get('ip', '')} {pc.get('titular', '')}".casefold()
            for pc in data
        }
        query = self.search_var.get().casefold().strip()
        self.filtered_list = [
            pc for pc in data
            if not query or query in self.search_index.get(id(pc), '')
        ]
        self._sort_records(self.filtered_list)
        self.visible_row_ids = ()
        self.last_search_query = query
        logging.debug(f"Datos cargados: {len(self.pc_list)} PCs")
        self.create_grid()
        if not self.window_size_set:
            self.adjust_window_to_content()
            self.window_size_set = True
        self._schedule_network_checks()
        self._refresh_status()

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
        self._refresh_status()

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
                        text=f'SSH :{result}' if result else '✗',
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
            self._sort_records(self.filtered_list)
            logging.debug(f"Lista ordenada por {column}, ascendente: {self.sort_ascending}")

            # Actualizar headers para mostrar el indicador de ordenamiento
            self.create_fixed_headers()

            # Actualizar la visualización
            # Optimización: durante ordenamiento evitamos lanzar comprobaciones de red
            self.update_grid_display(from_sort=True)

        except Exception as e:
            logging.error(f"Error al ordenar por {column}: {e}")

    def _sort_records(self, records):
        """Aplica el orden elegido sin cambiar su dirección."""
        if self.sort_column == 'ip':
            records.sort(
                key=lambda item: ip_vlan_host_sort_key(item.get('ip', '')),
                reverse=not self.sort_ascending,
            )
        elif self.sort_column:
            records.sort(
                key=lambda item: str(item.get(self.sort_column, '')).casefold(),
                reverse=not self.sort_ascending,
            )

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

        self._sort_records(filtered_list)
        visible_row_ids = tuple(id(pc) for pc in filtered_list)
        self.filtered_list = filtered_list
        if visible_row_ids == self.visible_row_ids:
            return

        logging.debug(f"Resultados del filtro: {len(filtered_list)} PCs")
        self.update_grid_display()
        self._refresh_status()

    def refresh_data(self):
        if self.data_loading:
            return

        logging.info("Refrescando datos desde Google Sheets")
        self.data_loading = True
        self.refresh_button.config(state='disabled')
        self._refresh_status()
        future = DATA_EXECUTOR.submit(fetch_pc_list)

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

        saved_geometry = self.ui_settings.get('geometry')
        if isinstance(saved_geometry, str) and 'x' in saved_geometry:
            self.geometry(saved_geometry)
        else:
            width = min(max(total_width, 700), self.winfo_screenwidth() - 80)
            height = min(max(total_height, 460), self.winfo_screenheight() - 100)
            x = (self.winfo_screenwidth() - width) // 2
            y = (self.winfo_screenheight() - height) // 2
            self.geometry(f"{width}x{height}+{x}+{y}")
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
                max_length = max(max_length, 11)  # Espacio para "SSH :49151"

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
                weight = 1 if col == 0 else 0
                self.headers_frame.grid_columnconfigure(col, minsize=width, weight=weight)
                self.scrollable_frame.grid_columnconfigure(col, minsize=width, weight=weight)

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

        if not self.filtered_list:
            message = 'No hay equipos que coincidan con el filtro.' if self.pc_list else 'La hoja no tiene equipos.'
            tk.Label(
                self.scrollable_frame,
                text=message,
                anchor='center',
                fg='#555555',
                pady=24,
            ).grid(row=0, column=0, columnspan=5, sticky='ew')
            self.after(100, self.sync_column_widths)
            return

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
            usuario_rdp = normalize_rdp_username(pc["usuario"])
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
