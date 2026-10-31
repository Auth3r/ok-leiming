"""Explicit diagnostic navigation only; never starts a challenge or workflow."""
from src.client_geometry import client_input_to_screen, validate_client_geometry


ROUTE = {'main_nav': ('HOME_CONFIRMED', 'NAV_CONFIRMED'),
         'nav_tab': ('NAV_CONFIRMED', 'ROGUE_ENTRY_VISIBLE'),
         'entry_button': ('ROGUE_ENTRY_VISIBLE', 'ROGUE_PREPARATION_CONFIRMED')}


class NavigationStep720p:
    def __init__(self, capture, recognizer, interaction, hwnd, pid, preflight):
        self.capture, self.recognizer, self.interaction = capture, recognizer, interaction
        self.hwnd, self.pid, self.preflight = hwnd, pid, preflight
        self.used = set()
        self.pending = None

    def observe(self):
        packet = self.capture.get_frame()
        return packet, self.recognizer.observe(packet, hwnd=self.hwnd, pid=self.pid)

    def click_once(self, action):
        if action not in ROUTE or action in self.used or self.pending is not None:
            raise ValueError('Only one confirmed navigation click per action is permitted')
        if action == 'nav_tab' and 'main_nav' not in self.used:
            raise ValueError('The homepage navigation transition must be confirmed first')
        packet, result = self.observe()
        target = result['matches'].get(action)
        if (result['state'] != ROUTE[action][0] or not target or not target['matched']
                or target['score'] < target['threshold']):
            raise ValueError(f'Navigation target not confirmed: {action} on {result["state"]}')
        if action == 'entry_button' and target.get('label') != '进入副本':
            raise ValueError('Only the verified non-challenge dungeon entrance is permitted')
        left, top, right, bottom = target['bbox']
        if not (0 <= left < right <= 1280 and 0 <= top < bottom <= 720):
            raise ValueError('Navigation target outside client')
        x, y = (left+right)//2, (top+bottom)//2
        self.preflight()
        validate_client_geometry(self.hwnd, expected_size=(1280, 720))
        screen = client_input_to_screen(self.hwnd, x, y)
        self.preflight()
        # Consume the action before dispatch: an exception never causes a retry.
        self.used.add(action)
        self.pending = action
        self.interaction.click(x, y, move_back=False, down_time=.05)
        after_packet, after = self.observe()
        return packet, after_packet, {'action':action, 'client_point':[x,y],
            'screen_point':list(screen), 'target':target, 'before':result, 'after':after,
            'expected_after':ROUTE[action][1]}

    def confirm_transition(self):
        if self.pending is None:
            raise ValueError('No navigation transition to confirm')
        packet, result = self.observe()
        if result['state'] != ROUTE[self.pending][1]:
            raise ValueError(f'Navigation transition unconfirmed: {result["state"]}')
        self.pending = None
        return packet, result
