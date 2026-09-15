"""Punto de entrada de iTool, conservado para desarrollo y PyInstaller."""

import logging

from itool_app.app import IToolApp


def main():
    logging.info("Iniciando iTool")
    application = IToolApp()
    application.mainloop()


if __name__ == '__main__':
    main()
