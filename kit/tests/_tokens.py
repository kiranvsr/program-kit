"""The vendor, model and project names the kit must never contain.

Each token is assembled from fragments so that no file in the repository, this one included, holds a
forbidden token literally. Every test that checks neutrality imports from here.

Every name is matched as a substring, case-insensitively, glued to anything; where an ordinary word contains
a short name, the text is reworded. DISTINCTIVE names are the long ones, SHORT names the three to five letter
ones. A hyphen inside a name also matches a space or an underscore (test_neutrality.py builds that pattern).
"""

DISTINCTIVE = (
    "Cl" + "aude",
    "Anthr" + "opic",
    "Open" + "AI",
    "Gem" + "ini",
    "ent-" + "harness",
    "Son" + "net",
)
ORIGIN = DISTINCTIVE[4]  # the originating project, the one name with a hyphen
SHORT = (
    "G" + "PT",
    "Co" + "dex",
    "Gr" + "ok",
    "Fa" + "ble",
    "Op" + "us",
)
FORBIDDEN = DISTINCTIVE + SHORT
