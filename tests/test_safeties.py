"""Unit tests for the safety rails: cooldowns and the circuit breaker."""

from self_healer.remediations import SafetyState


class FakeClock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_cooldown_blocks_then_expires():
    clock = FakeClock()
    s = SafetyState(clock=clock)
    assert s.cooldown_ok("p", ("Pod", "default", "x"), 30) is True
    s.note_action("p", ("Pod", "default", "x"))
    assert s.cooldown_ok("p", ("Pod", "default", "x"), 30) is False
    clock.advance(30 * 60 + 1)
    assert s.cooldown_ok("p", ("Pod", "default", "x"), 30) is True


def test_cooldown_is_per_policy_and_target():
    clock = FakeClock()
    s = SafetyState(clock=clock)
    s.note_action("p1", ("Pod", "default", "x"))
    assert s.cooldown_ok("p2", ("Pod", "default", "x"), 30) is True
    assert s.cooldown_ok("p1", ("Pod", "default", "y"), 30) is True


def test_breaker_trips_at_limit():
    clock = FakeClock()
    s = SafetyState(max_actions_per_hour=3, clock=clock)
    for i in range(3):
        assert s.breaker_ok() is True
        s.note_action("p", ("Pod", "default", "x%d" % i))
    assert s.breaker_ok() is False


def test_breaker_window_slides():
    clock = FakeClock()
    s = SafetyState(max_actions_per_hour=1, clock=clock)
    s.note_action("p", ("Pod", "default", "x"))
    assert s.breaker_ok() is False
    clock.advance(3601)
    assert s.breaker_ok() is True
