from grok2api.cookies import cookie_dict_to_playwright, parse_cookie_header, required_grok_cookie_score


def test_parse_cookie_header_keeps_embedded_equals():
    cookies = parse_cookie_header("auth_token=abc=123; ct0=xyz; empty=")
    assert cookies["auth_token"] == "abc=123"
    assert cookies["ct0"] == "xyz"
    assert "empty" in cookies


def test_parse_cookie_header_allows_cookie_prefix():
    cookies = parse_cookie_header("Cookie: auth_token=abc; ct0=def")
    assert cookies == {"auth_token": "abc", "ct0": "def"}


def test_cookie_score_counts_key_material():
    cookies = parse_cookie_header("auth_token=abc; ct0=def; twid=ghi")
    assert required_grok_cookie_score(cookies) == 3


def test_cookie_hint_targets_grok_and_x_domains():
    cookies = cookie_dict_to_playwright({"auth_token": "abc"})
    assert {item["domain"] for item in cookies} == {".grok.com", ".x.com", ".twitter.com"}
