"""Completeness checks shared by snapshot generation and retained D1 records."""


def patch_completeness_current(family, patch_count, version, patches=()):
    # Re-extract mixed Morphe inventories to establish the public loader's selection.
    unnamed = family == "Morphe:V1" and any(
        not (patch.get("name") or "").strip() for patch in patches
    )
    required = 1 if family == "Reseam" else 4 if unnamed else 2 if patch_count == 0 else 0
    public_name_missing = family == "Morphe:V1" and any(
        not (patch.get("name") or "").strip() and patch.get("loader_excluded") is not True
        for patch in patches
    )
    return not public_name_missing and isinstance(version, int) and version >= required


def history_patch_inventory(item):
    """Reassemble private helpers without moving public patch IDs."""
    public = item["patches"]
    helpers = item.get("internal_patches", [])
    positions = item.get("patch_positions")
    if not helpers or positions is None:
        return public + helpers
    size = len(public) + len(helpers)
    if (
        not isinstance(positions, list)
        or len(positions) != len(public)
        or any(type(position) is not int or not 0 <= position < size for position in positions)
        or len(set(positions)) != len(positions)
    ):
        raise ValueError("Invalid private patch inventory positions")
    indexed = dict(zip(positions, public, strict=True))
    remaining = iter(helpers)
    return [
        indexed[position] if position in indexed else next(remaining) for position in range(size)
    ]
