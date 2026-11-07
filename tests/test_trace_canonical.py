"""CTX-07b: the canonical form must tell an image from the same text alone.

``canonical_request`` is what two consecutive requests are compared on (prefix
cache, digest).  It predated images, so two requests that differed only by an
attached screenshot produced the same canonical string and the same digest.
"""

from __future__ import annotations

from ai_coding.core.trace import canonical_request
from ai_coding.domain.message import ChatMessage, ImageRef


def _message(**kwargs):
    return ChatMessage(role="user", content="看图", timestamp="", **kwargs)


def _image(size: int = 10) -> ImageRef:
    return ImageRef(path="shot.png", mime="image/png", name="shot.png", bytes=size)


def test_an_image_changes_the_canonical_form():
    plain = canonical_request(None, [_message()], "")
    with_image = canonical_request(None, [_message(images=(_image(),))], "")

    assert plain != with_image
    assert "|images:1" in with_image


def test_unmeasured_images_are_marked_as_zero():
    """An older session has no ``bytes``; it counts as zero, exactly as
    ``image_tokens`` does.  The canonical form must not claim a cost nobody
    measured."""
    text = canonical_request(None, [_message(images=(_image(0),))], "")

    assert "|images:0" in text


def test_two_images_are_counted_separately():
    text = canonical_request(
        None,
        [_message(images=(_image(1), _image(2)))],
        "",
    )

    assert "|images:2" in text


def test_a_message_without_images_is_unchanged():
    text = canonical_request(None, [_message()], "")

    assert "images:" not in text
