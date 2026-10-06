"""The vendor, model and project names the kit must never contain.

Each token is assembled from fragments so that no file in the repository, this one included, holds a
forbidden token literally. Every test that checks neutrality imports from here.

DISTINCTIVE names are unlikely to occur inside another word, so a neutrality check matches them as substrings
anywhere. SHORT names could occur inside ordinary words, so a check matches them unless an ASCII letter directly
precedes or follows (a digit or underscore next to one still counts). Matching is case-insensitive.
"""

DISTINCTIVE = (
    "Cl" + "aude",
    "Anthr" + "opic",
    "Open" + "AI",
    "Gem" + "ini",
    "ent-" + "harness",
    "Son" + "net",
)
SHORT = (
    "G" + "PT",
    "Co" + "dex",
    "Gr" + "ok",
    "Fa" + "ble",
    "Op" + "us",
)
FORBIDDEN = DISTINCTIVE + SHORT
