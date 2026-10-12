"""FFmpeg Studio - entry point."""
import os
import sys


def main() -> int:
    if os.name == "nt":  # own taskbar identity + proper notification title
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("FFmpegStudio.App")
        except Exception:
            pass

    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    import core
    from theme import ThemeManager, make_icon
    from ui import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName(core.APP_NAME)
    app.setOrganizationName(core.APP_ID)
    app.setStyle("Fusion")  # consistent look; our palette + stylesheet do the theming
    app.setWindowIcon(make_icon())
    app.setQuitOnLastWindowClosed(True)

    settings = QSettings(core.APP_ID, core.APP_ID)
    theme = ThemeManager(app, settings.value("theme", "system", type=str))  # default: follow the system
    win = MainWindow(app, theme, settings)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
