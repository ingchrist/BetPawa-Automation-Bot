from shared.markets import double_chance_winner


def test_home_win_is_1x():
    assert double_chance_winner(2, 0) == "1X"


def test_away_win_is_2x():
    assert double_chance_winner(0, 2) == "2X"


def test_draw_is_x():
    assert double_chance_winner(1, 1) == "X"


def test_zero_zero_draw_is_x():
    assert double_chance_winner(0, 0) == "X"
