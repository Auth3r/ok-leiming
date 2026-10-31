"""Roguelike timing only. Crafting and Workflow timing are unaffected."""
from dataclasses import dataclass
from enum import StrEnum
from src.roguelike_policy import RogueState as S


@dataclass(frozen=True)
class FastTiming:
    first_check: float = .15
    poll: float = .15
    ui_timeout: float = 1.2
    stable_retry_after: float = 3.0
    safe_action_attempts: int = 3
    retries: int = 1
    deployment_select_wait: float = .2
    deployment_double_gap: float = .08
    deployment_camera_timeout: float = 10.
    deploy_interval: float = 5.
    special_placements: int = 2
    load_timeout: float = 20.
    animation_timeout: float = 8.
    post_battle_timeout: float = 10.
    reward_timeout: float = 4.
    settlement_timeout: float = 6.
    battle_timeout: float = 300.
    battle_poll: float = .3


TIMING = FastTiming()


class ActionMode(StrEnum):
    RETRYABLE_STABLE_ACTION = 'RETRYABLE_STABLE_ACTION'
    FAST_ACTION = 'FAST_ACTION'
    DISMISS_OVERLAY = 'DISMISS_OVERLAY'
    ONE_SHOT_TRANSITION = 'ONE_SHOT_TRANSITION'


def state_class(state, event='', special_shop=False):
    if state == S.OUTPOST or (state == S.SHOP and not special_shop):
        return 'TEAM_SELECTION'
    if state == S.BATTLE:
        return 'LONG_WAIT' if event == 'running' else 'BATTLE_DEPLOYMENT'
    if state == S.TRANSIENT:
        return 'LONG_WAIT'
    return 'FAST_DETERMINISTIC'
