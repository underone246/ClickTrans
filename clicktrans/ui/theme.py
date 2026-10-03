"""配色。默认跟随系统（读注册表里的 AppsUseLightTheme）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    card: tuple[int, int, int]
    border: str
    text: str
    muted: str
    accent: str
    error: str
    divider: str
    is_light: bool


DARK = Palette(
    card=(28, 30, 35),
    border="rgba(255, 255, 255, 0.13)",
    text="#E8EBF0",
    muted="#8A929E",
    accent="#6FA8FF",
    error="#FF8A8A",
    divider="rgba(255, 255, 255, 0.08)",
    is_light=False,
)

LIGHT = Palette(
    card=(252, 252, 254),
    border="rgba(0, 0, 0, 0.10)",
    text="#1D2026",
    muted="#6B7280",
    accent="#2563EB",
    error="#C0392B",
    divider="rgba(0, 0, 0, 0.07)",
    is_light=True,
)


def system_uses_light() -> bool:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return bool(value)
    except Exception:
        return False


def pick(theme: str = "follow_system") -> Palette:
    if theme == "light":
        return LIGHT
    if theme == "dark":
        return DARK
    return LIGHT if system_uses_light() else DARK


def rgba(color: tuple[int, int, int], alpha: float) -> str:
    r, g, b = color
    return f"rgba({r}, {g}, {b}, {max(0.0, min(1.0, alpha)):.3f})"
