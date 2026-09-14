import platform
import logging

log = logging.getLogger(__name__)

def attach_interactive_desktop():
    """
    On Windows, ensure the current thread and process are attached to the
    interactive user desktop (winsta0\\default) and DPI-aware so screen
    capture and input control target the actual active user session.
    """
    if platform.system() != "Windows":
        return

    import ctypes
    user32 = ctypes.windll.user32

    # Set DPI awareness
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per monitor DPI aware
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

    WINSTA_ALL_ACCESS = 0x37F
    DESKTOP_ALL_ACCESS = 0x1FF

    try:
        h_winsta0 = user32.OpenWindowStationW("winsta0", False, WINSTA_ALL_ACCESS)
        if h_winsta0:
            user32.SetProcessWindowStation(h_winsta0)
        h_desk0 = user32.OpenDesktopW("default", 0, False, DESKTOP_ALL_ACCESS)
        if h_desk0:
            user32.SetThreadDesktop(h_desk0)
    except Exception as e:
        log.warning("Could not attach to winsta0\\default: %s", e)
