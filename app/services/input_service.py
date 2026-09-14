import ctypes
import mss
import pyautogui
import pyperclip
import logging
from app.core.security import is_safe_event
from app.core.windows_desktop import attach_interactive_desktop

log = logging.getLogger(__name__)

# Configure pyautogui
pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.001  # Minimal pause for responsiveness

class InputService:
    @staticmethod
    def move_to(x=0, y=0, x_ratio=None, y_ratio=None, monitor_index=1):
        """
        Move mouse on host system with multi-monitor, high-DPI, and ratio-based alignment.
        """
        attach_interactive_desktop()
        try:
            with mss.mss() as sct:
                monitors = sct.monitors
                max_index = len(monitors) - 1
                idx = max(1, min(int(monitor_index), max_index))
                mon = monitors[idx]
                left = mon['left']
                top = mon['top']
                width = mon['width']
                height = mon['height']

            if x_ratio is not None and y_ratio is not None:
                abs_x = int(left + float(x_ratio) * width)
                abs_y = int(top + float(y_ratio) * height)
            else:
                abs_x = int(left + float(x))
                abs_y = int(top + float(y))

            # Use Win32 SetCursorPos for accurate multi-monitor & 4K cursor movement
            ctypes.windll.user32.SetCursorPos(abs_x, abs_y)
        except Exception as e:
            log.error(f"Error in move_to: {e}")
            try:
                pyautogui.moveTo(int(x), int(y))
            except Exception:
                pass

    @staticmethod
    def mouse_down(button='left'):
        attach_interactive_desktop()
        try:
            pyautogui.mouseDown(button=button)
        except Exception as e:
            log.error(f"Error in mouse_down: {e}")

    @staticmethod
    def mouse_up(button='left'):
        attach_interactive_desktop()
        try:
            pyautogui.mouseUp(button=button)
        except Exception as e:
            log.error(f"Error in mouse_up: {e}")

    @staticmethod
    def click(button='left'):
        attach_interactive_desktop()
        try:
            pyautogui.click(button=button)
        except Exception as e:
            log.error(f"Error in click: {e}")

    @staticmethod
    def scroll(amount):
        attach_interactive_desktop()
        try:
            pyautogui.scroll(amount)
        except Exception as e:
            log.error(f"Error in scroll: {e}")

    @staticmethod
    def handle_keyboard(data):
        """
        Processes a keyboard event with sanitization.
        """
        attach_interactive_desktop()
        if not is_safe_event(data):
            log.warning(f"Blocked unsafe keyboard event: {data}")
            return False

        action = data.get('action')
        key = data.get('key')
        
        try:
            if action == 'keydown':
                pyautogui.keyDown(key)
            elif action == 'keyup':
                pyautogui.keyUp(key)
            return True
        except Exception as e:
            log.error(f"Error in keyboard action {action} for key {key}: {e}")
            return False

    @staticmethod
    def set_host_clipboard(text: str) -> None:
        """Write text to the host clipboard."""
        if not isinstance(text, str):
            return
        if len(text) > 1_000_000:          # 1 MB cap — prevent memory abuse
            log.warning("Clipboard push rejected — payload too large (%d bytes)", len(text))
            return
        try:
            pyperclip.copy(text)
        except Exception as e:
            log.warning("Clipboard write failed: %s", e)

    @staticmethod
    def get_host_clipboard() -> str:
        """Read the current host clipboard text."""
        try:
            text = pyperclip.paste()
            return text if isinstance(text, str) else ""
        except Exception as e:
            log.warning("Clipboard read failed: %s", e)
            return ""

    @staticmethod
    def release_all_keys():
        """Release stuck mouse buttons and modifier keys."""
        attach_interactive_desktop()
        try:
            pyautogui.mouseUp(button='left')
            pyautogui.mouseUp(button='right')
            pyautogui.mouseUp(button='middle')
            for key in ['ctrl', 'alt', 'shift', 'win']:
                pyautogui.keyUp(key)
        except Exception as e:
            log.error("Error in release_all_keys: %s", e)
