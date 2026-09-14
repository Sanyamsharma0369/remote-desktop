"""
Keyboard allowlist + blocklist for the WebSocket control handler.
Prevents Remote Code Execution via injected system shortcuts.
"""
from app.core.config import settings

# ─── Allowlist: all safe printable characters ──────────────────────────────
_PRINTABLE = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    " `~!@#$%^&*()-_=+[]{}|;':\",./<>?\\"
)

# ─── Safe special keys ──────────────────────────────────────────────────────
_SPECIAL_KEYS = {
    "enter", "backspace", "delete", "tab", "escape", "space",
    "up", "down", "left", "right",
    "home", "end", "pageup", "pagedown",
    "insert", "capslock", "numlock", "scrolllock",
    "printscreen", "pause",
    "f1","f2","f3","f4","f5","f6",
    "f7","f8","f9","f10","f11","f12",
    "ctrl", "shift", "alt",
    "volumeup", "volumedown", "volumemute",
    "mediaplaypause", "medianexttrack", "mediaprevtrack",
}

# ─── Modifier-safe combos ───────────────────────────────────────────────────
_SAFE_CTRL_COMBOS = {
    "a","b","c","d","e","f","g","h","i","j","k","l","m",
    "n","o","p","q","r","s","t","u","v","w","x","y","z",
    "0","1","2","3","4","5","6","7","8","9",
    "home","end","left","right","up","down",
    "backspace","delete","enter","tab",
}

_SAFE_ALT_COMBOS = {
    "left","right","up","down","home","end",
    "f4",          # controlled by ALLOW_ALT_F4 flag
    "tab",         # app switcher — generally safe
    "enter",       # open properties — generally safe
}

# ─── Always-blocked combos ──────────────────────────────────────────────────
_BLOCKED_COMBOS = frozenset({
    frozenset({"ctrl","alt","delete"}),        # Ctrl+Alt+Del
    frozenset({"ctrl","shift","escape"}),       # Task Manager
    frozenset({"meta","r"}),                   # Win+R Run dialog
    frozenset({"meta","x"}),                   # Win+X Power menu
    frozenset({"meta","d"}),                   # Win+D Show desktop
    frozenset({"meta","l"}),                   # Win+L Lock screen
    frozenset({"meta","s"}),                   # Win+S Search
    frozenset({"meta","e"}),                   # Win+E Explorer
    frozenset({"meta","i"}),                   # Win+I Settings
    frozenset({"meta","v"}),                   # Win+V Clipboard history
})

# ─── Always-blocked individual keys ────────────────────────────────────────
_BLOCKED_KEYS = frozenset({
    "meta", "super", "win",                    # Windows/Meta key alone
    "apps",                                    # Application/Context menu key
})


def is_safe_event(data: dict) -> bool:
    """
    Returns True if the keyboard event is safe to replay on the host.
    
    Expected data shape:
        {"type": "keydown", "key": "a", "modifiers": ["ctrl"]}
    """
    raw_key = str(data.get("key", "")).lower().strip()
    modifiers = {m.lower() for m in data.get("modifiers", [])}

    # ── 1. Block explicitly dangerous standalone keys ──
    if raw_key in _BLOCKED_KEYS:
        return False

    # ── 2. Block always-blocked combos ──
    combo = frozenset(modifiers | {raw_key})
    if combo in _BLOCKED_COMBOS:
        return False

    # ── 3. Apply config flags ──
    if not settings.ALLOW_ALT_F4:
        if raw_key == "f4" and "alt" in modifiers:
            return False
    if not settings.ALLOW_CTRL_SHIFT_ESC:
        if raw_key == "escape" and {"ctrl","shift"}.issubset(modifiers):
            return False

    # ── 4. No modifier keys — check printable + special allowlist ──
    if not modifiers:
        return raw_key in _PRINTABLE or raw_key in _SPECIAL_KEYS

    # ── 5. Ctrl+key — check safe ctrl combos ──
    if modifiers == {"ctrl"}:
        return raw_key in _SAFE_CTRL_COMBOS

    # ── 6. Alt+key — check safe alt combos ──
    if modifiers == {"alt"}:
        return raw_key in _SAFE_ALT_COMBOS

    # ── 7. Ctrl+Shift (e.g. Ctrl+Shift+Z for redo) — allow if key is safe ──
    if modifiers == {"ctrl","shift"}:
        return raw_key in _SAFE_CTRL_COMBOS

    # ── 8. Anything with Meta/Win — always block ──
    if "meta" in modifiers or "win" in modifiers or "super" in modifiers:
        return False

    # Default: deny unknown modifier combinations
    return False
