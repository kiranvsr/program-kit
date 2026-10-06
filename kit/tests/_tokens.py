"""The vendor, model and project names the kit must never contain.

Each token is assembled from fragments so that no file in the repository, this one included, holds a
forbidden token literally. Every test that checks neutrality imports FORBIDDEN from here.
"""

FORBIDDEN = (
    "Cl" + "aude",
    "Anthr" + "opic",
    "Open" + "AI",
    "G" + "PT",
    "Co" + "dex",
    "Gem" + "ini",
    "Gr" + "ok",
    "ent-" + "harness",
    "Fa" + "ble",
    "Op" + "us",
    "Son" + "net",
)
