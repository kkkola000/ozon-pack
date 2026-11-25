"""Compatibility exports; marketplace adapters and the module share one GS1 parser."""

from pack_v2.shared.gs1 import GS, Code, comparison_key, equivalent, looks_like, parse, short

__all__ = ["GS", "Code", "comparison_key", "equivalent", "looks_like", "parse", "short"]
