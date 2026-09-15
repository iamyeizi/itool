import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials


SCOPE = (
    'https://spreadsheets.google.com/feeds',
    'https://www.googleapis.com/auth/drive',
)


def fetch_pc_records(base_dir, spreadsheet_name='bd_pcs'):
    credential_path = os.path.join(base_dir, 'credential.json')
    credentials = ServiceAccountCredentials.from_json_keyfile_name(credential_path, SCOPE)
    client = gspread.authorize(credentials)
    return client.open(spreadsheet_name).sheet1.get_all_records()


def describe_sheet_error(error):
    """Resume fallos de Sheets sin exponer detalles sensibles en la UI."""
    message = str(error).casefold()
    if isinstance(error, FileNotFoundError) or 'credential' in message:
        return 'No se pudo leer la credencial de Google Sheets.'
    if any(marker in message for marker in ('403', 'forbidden', 'permission')):
        return 'Google Sheets rechazó el acceso: revisá los permisos.'
    if any(marker in message for marker in ('401', 'unauthorized', 'invalid grant', 'authentication')):
        return 'No se pudo autenticar con Google Sheets.'
    if isinstance(error, OSError) or any(marker in message for marker in ('timeout', 'connection', 'network')):
        return 'No se pudo conectar a Google Sheets. Revisá la red.'
    return 'No se pudo actualizar la hoja. Probá de nuevo.'
