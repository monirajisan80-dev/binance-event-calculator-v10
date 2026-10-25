"""Pure reward math — no I/O, easy to unit test."""
from dataclasses import dataclass, asdict


@dataclass
class PoolStats:
    participants: int
    total_volume: float         # V_total of rank > skip_top
    per_dollar: float           # R_per_dollar
    per_1000: float             # R_per_1000 (uncapped)
    cap: float
    volume_to_hit_cap: float    # volume at which a user reaches the cap

    def to_dict(self):
        return asdict(self)


def remaining_pool_stats(volumes: list[float], remaining_pool: float, cap: float) -> PoolStats:
    """volumes = trading volumes (USD) of ranks 1001+ only."""
    v_total = sum(v for v in volumes if v and v > 0)
    if v_total <= 0 or remaining_pool <= 0:
        return PoolStats(len(volumes), v_total, 0.0, 0.0, cap, 0.0)
    per_dollar = remaining_pool / v_total
    return PoolStats(
        participants=len(volumes),
        total_volume=v_total,
        per_dollar=per_dollar,
        per_1000=per_dollar * 1000,
        cap=cap,
        volume_to_hit_cap=cap / per_dollar,
    )


def user_reward(volume: float, stats: PoolStats) -> float:
    return min(volume * stats.per_dollar, stats.cap)


def fixed_slot_rewards(slots: list[dict]) -> list[dict]:
    """
    slots: [{"from":1,"to":1,"amount":10}, {"from":6,"to":20,"amount":15,"split":true}, ...]
    'split': amount is a bucket shared equally by the ranks in range; otherwise amount is per rank.
    """
    out = []
    for s in slots:
        n = s["to"] - s["from"] + 1
        each = s["amount"] / n if s.get("split") else s["amount"]
        out.append({**s, "per_user": each, "ranks": n})
    return out
