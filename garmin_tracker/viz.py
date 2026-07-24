"""Design tokens.

The single source of truth for the dashboard's visual language, taken from the
approved design. Charts are hand-built SVG (see ``charts.py``) rather than a
plotting library, because the design specifies its own mark shapes, spacing and
label placement — a chart library would fight every one of those.
"""

from __future__ import annotations

# --- Surfaces & ink ---------------------------------------------------------
BG = "#211c17"          # page
CARD = "#2a2420"        # card surface
CARD_2 = "#322b25"      # nested card
INK = "#f0e6d2"         # primary
INK_2 = "#d8c9ab"       # secondary
MUTED = "#8a7d68"       # labels, axis text
MUTED_2 = "#b3a48c"     # legend text
HAIR = "rgba(240,230,210,.08)"   # card border
GRID = "rgba(240,230,210,.06)"   # gridline
GRID_2 = "rgba(240,230,210,.09)" # track

ACCENT = "#c67139"
ACCENT_2 = "#e0a377"

# --- Status (reserved — only ever good/warning/serious/critical) ------------
STATUS = {"good": "#4e9d5b", "warning": "#d9a441",
          "serious": "#c96f3b", "critical": "#c94b3f"}

# Score bands.
BANDS = [(80, "#4e9d5b"), (65, "#93b356"), (50, "#d9a441"), (35, "#c96f3b")]
BAND_FLOOR = "#c94b3f"

# --- Intensity bands: one ordered olive ramp, light -> dark = easy -> hard ---
BAND_COLORS = {"low": "#d5dcbb", "moderate": "#8e9c6a", "high": "#49532f"}
BAND_TEXT = {"low": "#3a4126", "moderate": "#262c15", "high": "#d5dcbb"}
BAND_LABEL = {"low": "Low", "moderate": "Moderate", "high": "High"}

# --- Sports keep their hue in every chart -----------------------------------
SPORT_COLORS = {"swim": "#4da3d9", "bike": "#b678c8", "run": "#c67139"}
SPORT_NAMES = {"swim": "Swim", "bike": "Bike", "run": "Run"}
SPORT_ORDER = ["swim", "bike", "run"]

# --- Training-load series ---------------------------------------------------
CTL = "#ead9b8"
ATL = "#93a9c4"
LOAD_BAR = "#463d33"
TSB_POS = "rgba(138,154,106,.45)"
TSB_NEG = "rgba(164,103,78,.45)"
TSB_LINE = "#d8c9ab"

DISPLAY_FONT = "'Caprasimo', serif"
BODY_FONT = "'Figtree', sans-serif"


def band_color(v) -> str:
    """Score colour for a 0-100 value; muted when there is no value."""
    if v is None:
        return MUTED
    for threshold, colour in BANDS:
        if v >= threshold:
            return colour
    return BAND_FLOOR


def tint(hex_colour: str, alpha: float = 0.14) -> str:
    """A translucent wash of a solid colour, for pill backgrounds."""
    h = hex_colour.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def status_color(name: str) -> str:
    return STATUS.get(name, MUTED)
