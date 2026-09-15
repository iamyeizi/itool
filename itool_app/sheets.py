import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials


SCOPE = (
    'https://spreadsheets.google.com/feeds',
    'https://www.googleapis.com/auth/drive',
)


def get_pc_list(base_dir, spreadsheet_name='bd_pcs'):
    credential_path = os.path.join(base_dir, 'credential.json')
    credentials = ServiceAccountCredentials.from_json_keyfile_name(credential_path, SCOPE)
    client = gspread.authorize(credentials)
    return client.open(spreadsheet_name).sheet1.get_all_records()
