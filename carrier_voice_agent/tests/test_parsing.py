from lanevoice import parsing


def test_extract_load_id_variants():
    assert parsing.extract_load_id("about load L1001") == "L1001"
    assert parsing.extract_load_id("load 1002") == "L1002"
    assert parsing.extract_load_id("no numbers here") is None


def test_extract_mc_dot():
    assert parsing.extract_mc_dot("my MC is 123456") == ("MC", "123456")
    assert parsing.extract_mc_dot("USDOT 2000002") == ("DOT", "2000002")
    assert parsing.extract_mc_dot("it's 654321") == ("DOT", "654321")
    assert parsing.extract_mc_dot("no id") == (None, None)


def test_extract_money():
    assert parsing.extract_money("I need $2,100") == 2100
    assert parsing.extract_money("2050 dollars") == 2050
    assert parsing.extract_money("2.1k") == 2100
    assert parsing.extract_money("no price") is None
