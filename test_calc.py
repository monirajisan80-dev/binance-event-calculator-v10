from calc import remaining_pool_stats, user_reward, fixed_slot_rewards


def test_per_1000_and_cap():
    s = remaining_pool_stats([400_000, 600_000], 80, 0.05)
    assert s.total_volume == 1_000_000
    assert abs(s.per_dollar - 0.00008) < 1e-12
    assert abs(s.per_1000 - 0.08) < 1e-9
    assert user_reward(1_000, s) == 0.05  # 0.08 capped to 0.05


def test_fixed_split():
    r = fixed_slot_rewards([{"from": 6, "to": 20, "amount": 15, "split": True}])
    assert r[0]["per_user"] == 1.0
