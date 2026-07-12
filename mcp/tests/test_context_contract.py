from datetime import UTC, datetime

import steam_proactive


def test_in_game_context_exposes_wake_contract_and_preserves_payload() -> None:
    steam_proactive._last_wake_presence = "unknown"
    observed = datetime(2026, 7, 12, 8, tzinfo=UTC)

    context = steam_proactive._with_wake_contract(
        {
            "available": True,
            "realtime": {
                "online_status": "in-game",
                "currently_playing": "Game",
            },
            "games": [{"name": "Game"}],
        },
        observed_at=observed,
    )

    assert context["presence"] == "in_game"
    assert context["interruptibility"] == 0.1
    assert context["confidence"] == 0.9
    assert context["transition"] == ""
    assert context["payload"]["realtime"]["currently_playing"] == "Game"
    assert datetime.fromisoformat(context["expires_at"]) > observed


def test_steam_owner_emits_generic_transition() -> None:
    steam_proactive._last_wake_presence = "in_game"
    context = steam_proactive._with_wake_contract(
        {
            "available": True,
            "realtime": {"online_status": "offline"},
        },
        observed_at=datetime(2026, 7, 12, 9, tzinfo=UTC),
    )

    assert context["presence"] == "offline"
    assert context["interruptibility"] == 0.0
    assert context["transition"] == "in_game->offline"


def test_realtime_error_yields_low_confidence_unknown_context() -> None:
    context = steam_proactive._with_wake_contract(
        {
            "available": False,
            "realtime": {"error": "timeout"},
        },
        observed_at=datetime(2026, 7, 12, 9, tzinfo=UTC),
    )

    assert context["presence"] == "unknown"
    assert context["confidence"] == 0.1
    assert context["payload"]["realtime"]["error"] == "timeout"
