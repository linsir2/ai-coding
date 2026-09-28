from ai_coding.ui.body import BodyBuffer


def test_body_write_text():
    body = BodyBuffer()
    body.write("hello")
    assert "hello" in body.get_formatted_text()


def test_body_write_multiple():
    body = BodyBuffer()
    body.write("line1\n")
    body.write("line2\n")
    assert "line1" in body.get_formatted_text()
    assert "line2" in body.get_formatted_text()


def test_body_clear():
    body = BodyBuffer()
    body.write("hello")
    body.clear()
    assert "hello" not in body.get_formatted_text()


def test_body_write_with_style():
    body = BodyBuffer()
    body.write("error", style="class:error")
    assert "error" in body.get_formatted_text()
