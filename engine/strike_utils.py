import math


def round_half_up(value: float) -> int:
    if value >= 0:
        return int(math.floor(value + 0.5))
    return int(math.ceil(value - 0.5))


def round_to_nearest_step(value: float, step: int) -> int:
    if step <= 0:
        raise ValueError(f"step must be positive, got {step}")
    return int(round_half_up(value / step) * step)
