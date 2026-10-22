"""Control which strategies may open new positions."""

ENTRY_PERMISSIONS = {
    "hybrid": frozenset({"DCA", "SWING"}),
    "dca_only": frozenset({"DCA"}),
    "pause_entries": frozenset(),
}


def validate_strategy_mode(mode):
    if mode not in ENTRY_PERMISSIONS:
        raise ValueError(
            "strategy_mode must be hybrid, dca_only, or pause_entries."
        )


def entry_allowed(config, strategy):
    mode = config["strategy_mode"]
    validate_strategy_mode(mode)

    return strategy in ENTRY_PERMISSIONS[mode]


def cancel_disallowed_entries(state, config):
    """Cancel pending buys without touching positions or pending sells."""
    cancelled = []

    if not entry_allowed(config, "DCA"):
        if state.pending_dca is not None:
            cancelled.append(state.pending_dca.order_id)
            state.pending_dca = None

    if not entry_allowed(config, "SWING"):
        if state.pending_swing_buy is not None:
            cancelled.append(state.pending_swing_buy.order_id)
            state.pending_swing_buy = None

    return cancelled