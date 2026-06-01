from __future__ import annotations

from datetime import date

from task_management.kakao_export_adapter import parse_kakao_text_export


def test_kakao_bracket_export_parses_messages_and_aliases() -> None:
    messages = parse_kakao_text_export(
        """--------------- 2026년 5월 5일 화요일 ---------------
[나] [오전 8:55] 워크숍 이번주 목~일 중 하루 가야함
[팀원] [오후 1:05] 이번주 토요일 오후 가능
이어지는 줄
""",
        actor_aliases={"나": "me", "팀원": "teammate"},
        default_date=date(2026, 5, 1),
    )

    assert len(messages) == 2
    assert messages[0].sender_id == "me"
    assert messages[0].chat_id == "kakao/team"
    assert messages[0].visibility == "team"
    assert messages[0].received_at.isoformat() == "2026-05-05T08:55:00"
    assert messages[0].text == "워크숍 이번주 목~일 중 하루 가야함"
    assert messages[1].sender_id == "teammate"
    assert messages[1].received_at.isoformat() == "2026-05-05T13:05:00"
    assert messages[1].text == "이번주 토요일 오후 가능\n이어지는 줄"
    assert messages[0].message_id.startswith("kakao/")


def test_kakao_comma_export_parses_messages() -> None:
    messages = parse_kakao_text_export(
        "2026. 5. 5. 오후 8:30, 나 : 내일 보고서 자료 내가 챙길게",
        actor_aliases={"나": "me"},
    )

    assert len(messages) == 1
    assert messages[0].sender_id == "me"
    assert messages[0].received_at.isoformat() == "2026-05-05T20:30:00"
    assert messages[0].text == "내일 보고서 자료 내가 챙길게"
