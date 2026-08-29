"""A level a round has already bought must never be bought again in that round.

Phil, 2026-08-29: the buys table showed F2L2 twice inside one banked round --
bought at 11:30 and again at 14:42, out of the same cap. The saved rung came
back `PENDING` with `filled_at: None` despite having filled, so the ladder
collected it a second time. Rung status is a mutable flag on an object a
rebuild can replace; the FILLS are persisted and are what the money follows,
so they are the record these guards read.
"""

from datetime import date, datetime

from engine.fib_touch_ladder import FibTouchConfig, FibTouchLadder, PaperExecutor, TouchFill


def _ladder() -> FibTouchLadder:
    return FibTouchLadder(
        FibTouchConfig(
            symbol="NIFTY",
            side="CE",
            mother_timestamp=datetime(2026, 8, 28, 10, 15),
            lot_size=65,
            strike_step=50.0,
        ),
        premium_lookup=lambda *a: 200.0,
        expiry_source=lambda on: [date(2026, 9, 1)],
        executor=PaperExecutor(),
    )


def _fill(ladder: FibTouchLadder, *, level: int, fib_id: int, covered: list[str]) -> None:
    ladder.fills.append(
        TouchFill(
            buy_number=len(ladder.fills) + 1,
            level=level,
            timestamp=datetime(2026, 8, 28, 11, 30),
            index_price=24_117.85,
            premium=225.55,
            lots=1,
            quantity=65,
            strike=24_000.0,
            expiry=date(2026, 9, 1),
            option_type="CE",
            order_id="",
            fib_id=fib_id,
            covered=covered,
        )
    )


def test_a_bought_level_is_known_from_the_fills_not_the_rung_flag():
    ladder = _ladder()
    _fill(ladder, level=2, fib_id=2, covered=["F1L2", "F2L2"])
    assert ladder._bought_keys() == {"F1L2", "F2L2"}


def test_a_resurrected_rung_cannot_be_collected_or_bought_again():
    """The exact 2026-08-28 corruption: the rung is PENDING again after filling."""
    ladder = _ladder()
    _fill(ladder, level=2, fib_id=2, covered=["F1L2", "F2L2"])

    class _Rung:
        def __init__(self, key, status):
            self._key, self.status = key, status
            self.level, self.fib_id, self.index_price = 2, 2, 24_119.7
            self.armed, self.drawn_at, self.filled_at = True, None, None

        @property
        def key(self):
            return self._key

    revived = _Rung("F2L2", "COLLECTED")  # came back as if never bought
    fresh = _Rung("F2L3", "COLLECTED")  # a level this round has NOT bought
    ladder.rungs = [revived, fresh]

    collected = ladder._collected_rungs()
    keys = [r.key for r in collected]
    assert "F2L2" not in keys, "a level already bought this round was offered for buying again"
    assert "F2L3" in keys, "an unbought level must still be buyable"


def test_the_next_round_may_buy_the_same_level_again():
    """Banking a round clears the fills, which is the rounds rule Phil adjudicated."""
    ladder = _ladder()
    _fill(ladder, level=2, fib_id=2, covered=["F2L2"])
    assert "F2L2" in ladder._bought_keys()
    ladder.fills = []  # what banking a round does
    assert ladder._bought_keys() == set(), "a banked round must free its levels for the next one"
