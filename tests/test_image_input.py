"""TDD (r17/T5): images ride as message parts, bytes loaded at send time.

Session files store a path, not base64 (OpenCode stores base64 in the message;
we refuse the bloat).  A missing/unreadable file degrades to a text note in the
message, never a broken request.
"""

from __future__ import annotations

import base64

from ai_coding.ai.mapping import to_chat_message
from ai_coding.attachments import save_data_url
from ai_coding.domain.message import ChatMessage, ImageRef
from ai_coding.domain.session import _message_from_dict, _message_to_dict

#: 1x1 transparent PNG
_PNG = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6360000002000154a24f5f0000000049454e44ae426082"
    )
).decode("ascii")


def test_a_data_url_becomes_a_path_backed_reference(tmp_path):
    ref = save_data_url(tmp_path, "shot", f"data:image/png;base64,{_PNG}")

    assert ref is not None
    assert ref.mime == "image/png"
    assert ref.path.startswith(".ai-coding/attachments/")
    assert (tmp_path / ref.path).is_file()


def test_a_non_image_data_url_is_refused(tmp_path):
    assert save_data_url(tmp_path, "x", "data:text/plain;base64,aGk=") is None


def test_the_message_round_trips_through_session_json():
    msg = ChatMessage(
        role="user",
        content="看这个",
        timestamp="t",
        images=(ImageRef(path=".ai-coding/attachments/x.png", mime="image/png", name="x"),),
    )

    back = _message_from_dict(_message_to_dict(msg))

    assert back.images == msg.images


def test_messages_without_images_keep_the_old_shape():
    msg = ChatMessage(role="user", content="hi", timestamp="t")

    assert "images" not in _message_to_dict(msg)


def test_mapping_emits_openai_content_parts_with_a_loader():
    msg = ChatMessage(
        role="user",
        content="hi",
        timestamp="t",
        images=(ImageRef(path="p.png", mime="image/png", name="p"),),
    )

    out = to_chat_message(msg, image_loader=lambda ref: "data:image/png;base64,AAA")

    assert out["content"] == [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
    ]


def test_mapping_stays_text_only_without_images():
    msg = ChatMessage(role="user", content="hi", timestamp="t")

    assert to_chat_message(msg)["content"] == "hi"


def test_an_unreadable_image_degrades_to_a_note():
    msg = ChatMessage(
        role="user",
        content="hi",
        timestamp="t",
        images=(ImageRef(path="gone.png", mime="image/png", name="gone"),),
    )

    out = to_chat_message(msg, image_loader=lambda ref: None)

    assert isinstance(out["content"], list)
    assert any(
        "无法读取" in part.get("text", "") and "gone" in part.get("text", "")
        for part in out["content"]
    ), "the unreadable image is named in a text part, wherever it lands"
