def format_status_text(equipment_count, last_update, complete_checks, total_checks, loading):
    sheet_status = (
        'Cargando hoja…'
        if loading and not last_update
        else f'{equipment_count} equipos · hoja {last_update or "sin actualizar"}'
    )
    if not total_checks:
        return sheet_status
    prefix = 'Comprobando' if loading else 'Red'
    return f'{sheet_status} · {prefix}: {complete_checks}/{total_checks}'


class HoverTooltip:
    """Tooltip liviano cuyo texto puede cambiar durante la ejecución."""

    def __init__(self, widget, text_provider):
        self.widget = widget
        self.text_provider = text_provider
        self.window = None
        widget.bind('<Enter>', self.show, add='+')
        widget.bind('<Leave>', self.hide, add='+')

    def show(self, _event=None):
        text = self.text_provider()
        if not text or self.window is not None:
            return
        self.window = window = tk.Toplevel(self.widget)
        window.wm_overrideredirect(True)
        window.wm_geometry(f'+{self.widget.winfo_rootx() + 18}+{self.widget.winfo_rooty() + 24}')
        tk.Label(window, text=text, background='#ffffe0', relief='solid', borderwidth=1).pack(
            padx=4,
            pady=2,
        )

    def hide(self, _event=None):
        if self.window is not None:
            self.window.destroy()
            self.window = None
import tkinter as tk
