"""request_compat.py — safe accessor for request_info payloads.

A response_handler's original_request may arrive as either the original
dataclass instance (if no checkpoint hop happened) or a plain dict (after
a real pause -> resume round-trip, since MAF doesn't reliably reconstruct
custom dataclasses across that boundary). Use this wherever a
response_handler reads fields off original_request.
"""

def rget(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)