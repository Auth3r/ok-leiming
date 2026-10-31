"""Explicit reference-image ROI -> client-image physical pixel bbox.

This pure helper does not infer a resolution, change a template, use DPI/Qt,
or enable production tasks. Matching/UI layout still needs image evidence.
Bboxes are half-open: floor left/top and ceil right/bottom to retain coverage.
"""
from numbers import Integral

REFERENCE_SIZE = (3840, 2160)


def _integers(values, count, label):
    try:
        values = tuple(values)
    except TypeError as error:
        raise ValueError(f'{label} must contain {count} integer pixels') from error
    if len(values) != count or any(isinstance(v, bool) or not isinstance(v, Integral) for v in values):
        raise ValueError(f'{label} must contain {count} integer pixels')
    return tuple(int(v) for v in values)


def reference_bbox_to_client(bbox, client_size, *, reference_size=REFERENCE_SIZE):
    """Map an explicitly chosen reference ROI into a known client image size.

    Reject invalid source ROIs/sizes; never clamp or guess coordinate semantics.
    Integer arithmetic avoids floating-point boundary errors at either edge.
    """
    rw, rh = _integers(reference_size, 2, 'Reference size')
    cw, ch = _integers(client_size, 2, 'Client size')
    if min(rw, rh, cw, ch) <= 0:
        raise ValueError('Reference and client sizes must be positive')
    left, top, right, bottom = _integers(bbox, 4, 'Reference bbox')
    if not (0 <= left < right <= rw and 0 <= top < bottom <= rh):
        raise ValueError(f'Reference bbox outside {(rw, rh)}: {(left, top, right, bottom)}')
    return (left * cw // rw, top * ch // rh,
            (right * cw + rw - 1) // rw, (bottom * ch + rh - 1) // rh)
