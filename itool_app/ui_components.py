def format_status_text(
    equipment_count,
    visible_count,
    last_update,
    complete_checks,
    total_checks,
    sheet_loading,
    network_loading,
    sheet_error=None,
):
    if sheet_error and not last_update:
        sheet_status = 'Sin datos cargados'
    elif sheet_loading and not last_update:
        sheet_status = 'Cargando hoja…'
    else:
        sheet_status = f'{equipment_count} equipos · {visible_count} visibles'
        if last_update:
            sheet_status += f' · hoja {last_update}'

    details = []
    if sheet_error:
        details.append(sheet_error)
    elif sheet_loading and last_update:
        details.append('Actualizando hoja…')
    if total_checks:
        prefix = 'Comprobando red' if network_loading else 'Red'
        details.append(f'{prefix}: {complete_checks}/{total_checks}')
    return ' · '.join((sheet_status, *details))
