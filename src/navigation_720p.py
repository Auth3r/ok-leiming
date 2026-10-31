"""Read-only public navigation evidence; does not enable the guarded runner."""
from src.app_paths import APP_ROOT
import json
from pathlib import Path

import cv2
import numpy as np

from src.recognition_geometry import reference_bbox_to_client
from src.native_matching import match_native


class Navigation720p:
    """Match native assets only. No input, OCR, resizing or navigation scheduler."""

    def __init__(self, root=None):
        root = Path(root) if root is not None else APP_ROOT / 'templates/navigation_720p'
        self.root=root
        self.revision=(root/'manifest.json').stat().st_mtime_ns
        manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
        if manifest['client_size'] != [1280, 720]:
            raise ValueError('Navigation assets must be native 1280x720')
        self.specs = manifest['templates']
        self.images = {}
        for name, spec in self.specs.items():
            image = cv2.imdecode(np.fromfile(root / spec['file'], dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            if image is None or image.size == 0 or image.std() < 1:
                raise ValueError(f'Invalid navigation template: {name}')
            roi = reference_bbox_to_client(spec['reference_roi'], (1280, 720),
                                           reference_size=spec['reference_size'])
            if image.shape[1] > roi[2] - roi[0] or image.shape[0] > roi[3] - roi[1]:
                raise ValueError(f'Navigation template outside ROI: {name}')
            if not 0 < spec['threshold'] <= 1:
                raise ValueError(f'Invalid navigation threshold: {name}')
            if spec.get('score_mode', 'luma') not in ('luma', 'antialias_luma'):
                raise ValueError(f'Invalid navigation score mode: {name}')
            self.images[name] = image

    def observe(self, packet, *, hwnd, pid):
        image, metadata = packet.image, packet.metadata
        if (not isinstance(image, np.ndarray) or image.shape != (720, 1280, 3)
                or image.dtype != np.uint8):
            raise ValueError('Navigation requires native 1280x720 BGR pixels')
        if (metadata['backend'] != 'WGC' or metadata['pixel_format'] != 'BGR'
                or metadata['hwnd'] != hwnd or metadata['pid'] != pid
                or hwnd <= 0 or pid <= 0 or metadata['sequence'] <= 0
                or not metadata['session_id']
                or metadata['system_relative_time'] <= metadata['request_qpc']):
            raise ValueError('Invalid navigation capture provenance')
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        matches = {}
        for name, spec in self.specs.items():
            roi = reference_bbox_to_client(spec['reference_roi'], (1280, 720),
                                           reference_size=spec['reference_size'])
            left, top, right, bottom = roi
            template = self.images[name]
            patch = gray[top:bottom, left:right]
            mode = spec.get('score_mode', 'luma')
            score, location, raw_score = match_native(patch, template, spec['threshold'],
                                                      antialias=mode == 'antialias_luma')
            x, y = location
            matches[name] = {'target': name, 'score': float(score), 'threshold': spec['threshold'],
                'roi': list(roi), 'bbox': [left+x, top+y, left+x+template.shape[1], top+y+template.shape[0]],
                'matched': bool(score >= spec['threshold']), 'label': spec.get('label'),
                'raw_score': float(raw_score), 'score_mode': mode}
        # Transparent homepage labels have native variants for observed backgrounds.
        def present(name):
            return any(match['matched'] for key, match in matches.items()
                       if self.specs[key].get('target', key) == name)
        overlay = any(present(name) for name, spec in self.specs.items() if spec['role'] == 'overlay')
        states = []
        # This route only opens navigation. Its own opaque target is sufficient;
        # unrelated transparent black-shield/daily labels vary with home scenes.
        # The caller still verifies NAV/ENTRY after clicking, and overlays win.
        if present('main_nav'):
            states.append('HOME_CONFIRMED')
        if all(present(name) for name in ('nav_title', 'nav_recommend')):
            states.append('NAV_CONFIRMED')
        if all(present(name) for name in ('entry_title', 'entry_tab', 'entry_button')):
            states.append('ROGUE_ENTRY_VISIBLE')
        state = 'BLOCKED_BY_OVERLAY' if overlay else states[0] if len(states) == 1 else 'UNKNOWN'
        targets={}
        for key,match in matches.items():
            target=self.specs[key].get('target',key)
            if match['matched'] and (target not in targets or match['score']>targets[target]['score']):
                targets[target]=match
        return {'state': state, 'hwnd': hwnd, 'pid': pid, 'backend': metadata['backend'],
                'image_size': [1280, 720], 'frame_provenance': dict(metadata),
                'matches': matches,'targets':targets,
                'entry_bbox': targets['entry_button']['bbox'] if state == 'ROGUE_ENTRY_VISIBLE' else None}
