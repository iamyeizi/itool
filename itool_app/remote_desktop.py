def normalize_rdp_username(username):
    """Conserva el formato de usuario indicado para la conexión RDP remota.

    En el cliente de Escritorio remoto, ``.\\usuario`` identifica una cuenta
    local del *cliente*, no necesariamente una cuenta local del equipo remoto.
    Por eso un nombre simple debe enviarse sin prefijo: el servidor remoto lo
    resuelve como su propia cuenta local.
    """
    return str(username).strip()
