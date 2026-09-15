def normalize_rdp_username(username):
    """Explicita que una cuenta simple pertenece al equipo RDP de destino."""
    username = str(username).strip()
    if '\\' in username or '@' in username:
        return username
    return f'.\\{username}'
