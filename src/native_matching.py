"""Optional edge-rasterization normalization for calibrated native UI assets."""
import cv2
import numpy as np


def panel_evidence(frame, bounds, interior):
    """Measure four visible panel edges, independent of translucent scenery.

    The caller supplies calibrated client rectangles, never screen coordinates.
    Both the panel boundary and its uninterrupted lower body must be visible.
    """
    if (len(bounds)!=4 or len(interior)!=4 or
            any(type(v) is not int for v in (*bounds,*interior))):
        return {'matched': False, 'reason': 'invalid panel coordinates'}
    x,y,r,b = bounds
    a,c,d,e = interior
    h,w = frame.shape[:2]
    if not (4 <= x < r <= w-4 and 4 <= y < b <= h-4 and
            r-x>30 and b-y>30 and x<a<d<r and y<c<e<b):
        return {'matched': False, 'reason': 'panel outside client'}
    pixels = frame.astype('float32')
    differences = (
        pixels[y+3:y+6,x+15:r-15].mean(0)-pixels[y-4:y-1,x+15:r-15].mean(0),
        pixels[b-5:b-2,x+15:r-15].mean(0)-pixels[b+1:b+4,x+15:r-15].mean(0),
        pixels[y+15:b-15,x+3:x+6].mean(1)-pixels[y+15:b-15,x-4:x-1].mean(1),
        pixels[y+15:b-15,r-5:r-2].mean(1)-pixels[y+15:b-15,r+1:r+4].mean(1))
    edges = [float(np.median(np.linalg.norm(d,axis=1))) for d in differences]
    body = frame[c:e,a:d]
    coverage = float(np.mean(np.max(body,axis=2)>0)) if body.size else 0.
    # Actual selected/unselected cards: every median edge contrast >=7 BGR
    # units; absent panels have no edge. Do not correlate changing scenery.
    return {'matched': min(edges)>=4 and coverage>=.99,
            'edge_contrast': edges, 'body_coverage': coverage}


def match_native(patch, template, threshold, *, difference=False, antialias=False):
    method = cv2.TM_SQDIFF_NORMED if difference else cv2.TM_CCOEFF_NORMED

    def measure(a, b):
        minimum, maximum, min_at, max_at = cv2.minMaxLoc(cv2.matchTemplate(a, b, method))
        return (1-minimum, min_at) if difference else (maximum, max_at)

    raw_score, location = measure(patch, template)
    score = raw_score
    if antialias and raw_score < threshold:
        # Same physical pixel grid and colour space on both sides. Filtering
        # removes one-pixel edge coverage differences; it never rescales ROIs,
        # changes thresholds, or removes colour/brightness from difference mode.
        score, location = measure(cv2.GaussianBlur(patch, (5, 5), 1.0),
                                  cv2.GaussianBlur(template, (5, 5), 1.0))
    return float(score), location, float(raw_score)
