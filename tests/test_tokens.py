from bindery.tokens import estimate_tokens


def test_empty_is_zero():
    assert estimate_tokens("") == 0


def test_non_empty_is_never_zero():
    """A budget loop that sees a cost of 0 would never terminate."""
    assert estimate_tokens(".") >= 1


def test_japanese_costs_more_per_character_than_latin():
    japanese = estimate_tokens("認証方式の決定")
    latin = estimate_tokens("auth method decision")
    assert japanese > latin // 2
    assert estimate_tokens("あ" * 100) == 100


def test_latin_is_roughly_four_characters_per_token():
    assert estimate_tokens("a" * 400) == 100
