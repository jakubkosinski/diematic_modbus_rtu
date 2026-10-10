import pytest

from diematic.transport.backoff import Backoff


class TestBackoff:
    def test_grows_and_caps(self):
        backoff = Backoff(initial=1, maximum=5, factor=2, jitter=0, rng=lambda: 0.5)
        assert [backoff.next_delay() for _ in range(5)] == [1, 2, 4, 5, 5]

    def test_reset(self):
        backoff = Backoff(initial=1, maximum=8, jitter=0, rng=lambda: 0.5)
        backoff.next_delay()
        backoff.next_delay()
        backoff.reset()
        assert backoff.next_delay() == 1

    def test_jitter_stays_within_bounds(self):
        low = Backoff(initial=10, jitter=0.2, rng=lambda: 0.0).next_delay()
        high = Backoff(initial=10, jitter=0.2, rng=lambda: 1.0).next_delay()
        assert (low, high) == pytest.approx((8, 12))
