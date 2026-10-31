"""Read visible assigned-today rows; no navigation or dispatch input.

A caller must prove list coverage and re-observe the final rows before using
these records as a completed-day result. One frame never proves quota exhaustion.
All reported boxes use the task's reference frame, shared by 720P and 4K.
"""
import re

import cv2
import numpy as np


_TIMER = re.compile(r'(\d{2}):([0-5]\d):([0-5]\d)')
_POWER = re.compile(r'[Xx×]?(\d{3,6})')


def _text(box):
    return re.sub(r'\s+', '', str(box.name))


def _roi(box):
    return [round(box.x), round(box.y), round(box.x + box.width), round(box.y + box.height)]


def _center_y(box):
    return box.y + box.height / 2


def _labels(task, left, right, threshold):
    return [box for box in task.ocr(x=left, y=.16, to_x=right, to_y=.94,
                                  frame=task.frame, threshold=threshold)
            if box.confidence >= threshold]


def _portrait(task, today, power):
    """Inspect inside the matching card, excluding its icon and power label."""
    reference = task.frame
    native = getattr(task, 'native_frame', None)
    pixels = native if native is not None else reference
    height, width = reference.shape[:2]
    center = power.x + power.width / 2
    bounds = [center - .017 * width, today.y + .028 * height,
              center + .017 * width, power.y - .022 * height]
    if bounds[3] - bounds[1] < .022 * height:
        return None
    sx, sy = pixels.shape[1] / width, pixels.shape[0] / height
    left, top, right, bottom = [round(bounds[0] * sx), round(bounds[1] * sy),
                              round(bounds[2] * sx), round(bounds[3] * sy)]
    if not (0 <= left < right <= pixels.shape[1] and 0 <= top < bottom <= pixels.shape[0]):
        return None
    crop = pixels[top:bottom, left:right]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    deviation = float(np.std(gray))
    # Both a face crop and its recorded numeric power must exist on this row.
    if deviation < 5:
        return None
    return {'roi': [round(value) for value in bounds],
            'client_roi': [left, top, right, bottom], 'stddev': round(deviation, 3)}


def read_assigned_today(task, vision):
    """Return independently readable current-page rows, excluding unknown rows.

    Do not infer professions or fixed names. Pair the today label, name, timer
    and at least one pictured member's power by their current row positions.
    Duplicate or ambiguous names are excluded rather than combined into proof.
    """
    if not vision.page('BLACK_SHIELD'):
        return []
    height, width = task.frame.shape[:2]
    todays = [box for box in _labels(task, .42, .475, .95) if _text(box) == '今日']
    names = _labels(task, .47, .65, .95)
    timers = _labels(task, .42, .52, .93)
    power_proposals = _labels(task, .69, .89, .01)
    powers = [box for box in power_proposals if box.confidence>=.9]
    for box in power_proposals:
        if box.confidence>=.9 or not re.fullmatch(r'[Xx×]\d{3,6}',_text(box)):continue
        # Crossed-blades artwork may be merged with the digits. Its weak OCR
        # only locates the field; independently read the numeric part at .9.
        left=box.x+box.width*.22;top=max(0,box.y-.006*height)
        right=min(width,box.x+box.width+.005*width)
        bottom=min(height,box.y+box.height+.006*height)
        reread=[b for b in task.ocr(x=left/width,y=top/height,to_x=right/width,to_y=bottom/height,
                                   frame=task.frame,threshold=.9)
                if b.confidence>=.9 and re.fullmatch(r'\d{3,6}',_text(b))]
        if len(reread)==1:powers.append(reread[0])
    rows = []
    for today in sorted(todays, key=lambda box: box.y):
        same_names = [box for box in names
                      if abs(_center_y(box) - _center_y(today)) <= .022 * height
                      and 2 <= len(_text(box)) <= 24 and _text(box) != '今日'
                      and not re.fullmatch(r'[\d:：/／]+', _text(box))]
        same_timers = [box for box in timers
                       if .055 * height <= _center_y(box) - _center_y(today) <= .125 * height
                       and _TIMER.fullmatch(_text(box))]
        if len(same_names) != 1 or len(same_timers) != 1:
            continue
        name, timer = same_names[0], same_timers[0]
        digits = _TIMER.fullmatch(_text(timer)).groups()
        if not any(int(value) for value in digits):
            continue  # 00:00:00 requires the separate claim-state inspection.
        card_evidence = []
        for power in powers:
            match = _POWER.fullmatch(_text(power))
            if not match or not int(match.group(1)):
                continue
            if abs(_center_y(power) - _center_y(timer)) > .035 * height:
                continue
            portrait = _portrait(task, today, power)
            if portrait is not None:
                card_evidence.append({'power': int(match.group(1)), 'power_roi': _roi(power),
                                      'power_confidence': float(power.confidence),
                                      'portrait': portrait})
        if not card_evidence:
            continue
        top = max(0, round(today.y - .022 * height))
        bottom = round(max(timer.y + timer.height,
                           *(card['power_roi'][3] for card in card_evidence)) + .015 * height)
        if bottom > .94 * height:
            continue
        rows.append({'name': _text(name), 'timer': _text(timer),
                     'powers': [card['power'] for card in card_evidence],
                     'row_roi': [round(.325 * width), top, round(.975 * width), bottom],
                     'today': {'roi': _roi(today), 'confidence': float(today.confidence)},
                     'task_name': {'roi': _roi(name), 'confidence': float(name.confidence)},
                     'countdown': {'roi': _roi(timer), 'confidence': float(timer.confidence)},
                     'cards': card_evidence})
    counts = {}
    for row in rows:
        counts[row['name']] = counts.get(row['name'], 0) + 1
    return [row for row in rows if counts[row['name']] == 1]
