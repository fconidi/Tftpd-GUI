"""Look of the window: one CSS sheet built on the active GTK theme.

Colours come from the theme (``@theme_*``) so light and dark themes both
work; only the accent and the signal colours are fixed.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

CSS = b"""
@define-color accent #0e7c92;
@define-color accent_hover #12a0bb;
@define-color signal #1fb870;
@define-color danger #c0283c;

window { background-color: @theme_bg_color; }

notebook > header { background: transparent; border-bottom: 1px solid alpha(@theme_fg_color, 0.12); padding: 0 12px; }
notebook > header > tabs > tab {
  padding: 9px 20px; margin: 0 2px; border: none; border-bottom: 3px solid transparent;
  font-weight: 600; min-width: 64px; opacity: 0.75;
  transition: opacity 120ms ease-out, border-color 120ms ease-out;
}
notebook > header > tabs > tab:hover { opacity: 1; }
notebook > header > tabs > tab:checked { opacity: 1; border-bottom-color: @accent; }
notebook > stack { background: transparent; }

.section-title { font-weight: 700; font-size: 12px; letter-spacing: 0.4px; margin-top: 10px; opacity: 0.85; }
.hint { font-size: 11px; opacity: 0.7; }

entry, spinbutton { min-height: 32px; border-radius: 6px; }
entry:focus, spinbutton:focus { border-color: @accent; box-shadow: 0 0 0 2px alpha(@accent, 0.25); }
combobox button { min-height: 32px; border-radius: 6px; font-weight: 400; }

button { min-height: 32px; padding: 0 16px; border-radius: 6px; font-weight: 600; }
button.suggested-action {
  background-image: linear-gradient(to bottom, @accent_hover, @accent);
  color: #ffffff; border-color: shade(@accent, 0.8); text-shadow: none;
  box-shadow: 0 1px 2px alpha(black, 0.25);
}
button.suggested-action:hover { background-image: linear-gradient(to bottom, shade(@accent_hover, 1.1), @accent_hover); }
button.suggested-action:disabled { background-image: none; background-color: alpha(@accent, 0.35); color: alpha(#ffffff, 0.7); box-shadow: none; }
button.destructive-action {
  background-image: linear-gradient(to bottom, shade(@danger, 1.15), @danger);
  color: #ffffff; border-color: shade(@danger, 0.8); text-shadow: none;
  box-shadow: 0 1px 2px alpha(black, 0.25);
}
button.destructive-action:disabled { background-image: none; background-color: alpha(@theme_fg_color, 0.1); color: alpha(@theme_fg_color, 0.4); border-color: alpha(@theme_fg_color, 0.12); box-shadow: none; }

switch:checked { background-color: @accent; border-color: shade(@accent, 0.8); }
switch:checked slider { border-color: shade(@accent, 0.8); }

scrolledwindow.list { border: 1px solid alpha(@theme_fg_color, 0.14); border-radius: 8px; }
treeview { font-feature-settings: "tnum"; }
treeview.view:selected, treeview.view:selected:focus { background-color: alpha(@accent, 0.9); color: #ffffff; }
treeview header button { font-weight: 700; font-size: 11px; min-height: 28px; padding: 0 10px; border-radius: 0; }
treeview.log-view { font-family: "DejaVu Sans Mono", monospace; font-size: 11px; }

.dhcp-banner {
  background-image: linear-gradient(to right, shade(@danger, 0.85), @danger);
  color: #ffffff; padding: 11px 16px; border-radius: 8px; font-weight: 700;
  box-shadow: 0 2px 6px alpha(@danger, 0.45);
}

.status-bar { border-top: 1px solid alpha(@theme_fg_color, 0.12); padding: 6px 14px; background-color: alpha(@theme_fg_color, 0.04); }
.status-bar label { font-size: 12px; }
.status-dot { min-width: 10px; min-height: 10px; border-radius: 50%; background-color: alpha(@theme_fg_color, 0.3); margin-right: 8px; }
.status-dot.on { background-color: @signal; box-shadow: 0 0 0 3px alpha(@signal, 0.28); }

infobar { border-radius: 0; }
infobar label { font-weight: 600; }
"""


def apply() -> None:
    """Load the style sheet for the default screen (safe to call twice)."""
    screen = Gdk.Screen.get_default()
    if screen is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def add_class(widget: Gtk.Widget, *names: str) -> Gtk.Widget:
    context = widget.get_style_context()
    for name in names:
        context.add_class(name)
    return widget
