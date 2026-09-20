from ai_coding.infra import json_utils as ju


def test_to_json_from_json_roundtrip():
    obj = {"a": 1, "b": "x"}
    assert ju.from_json(ju.to_json(obj), dict) == obj


def test_to_json_pretty_indents():
    assert "\n" in ju.to_json_pretty({"k": "v"})


def test_is_valid_json():
    assert ju.is_valid_json('{"a":1}')
    assert not ju.is_valid_json("{oops")


def test_pretty_print():
    assert ju.pretty_print('{"a":1}') == '{\n  "a": 1\n}'


def test_json_file_io(tmp_path):
    p = tmp_path / "nested" / "d.json"
    ju.to_json_file(p, {"x": [1, 2]})
    assert ju.from_json_file(p) == {"x": [1, 2]}
