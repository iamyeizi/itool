# iTool

Cliente de escritorio para consultar equipos desde Google Sheets y abrir conexiones RDP o SSH.

## Estructura

- `main.py`: punto de entrada para desarrollo y PyInstaller.
- `itool_app/app.py`: aplicación Tkinter y coordinación de tareas en segundo plano.
- `itool_app/networking.py`: ping, comprobación de puertos y caché SSH.
- `itool_app/remote_desktop.py`: normalización del usuario RDP.
- `itool_app/sheets_source.py`: lectura de la hoja de equipos.
- `itool_app/ui_components.py`: componentes de interfaz reutilizables.
- `tests/`: pruebas unitarias de los módulos sin interfaz.
- `utils/`: iconos y plantilla RDP.

## Desarrollo

Instalá las dependencias con Pipenv y ejecutá la aplicación:

```bash
pipenv install
pipenv run python main.py
```

Para correr las pruebas:

```bash
pipenv run python -m unittest discover -s tests -v
```

En Windows, el ejecutable se genera con `./build_win.ps1`; por defecto crea una carpeta (`--onedir`) para iniciar más rápido. Usá `./build_win.ps1 -OneFile` solo si necesitás un único archivo.

No se versiona `credential.json`: crealo localmente a partir de `credential.example.json` con las credenciales autorizadas para la hoja.
