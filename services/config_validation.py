"""Validate settings before downloading data or changing any balances."""
# Import finite-number checks and the real-number type used by configuration validation.
from math import isclose, isfinite
from numbers import Real


# Validate a real numeric setting against optional lower and upper bounds.
def number(value, name, *, minimum=0, strict=False, maximum=None):
    # Reject Booleans, nonnumeric values, infinity, and NaN.
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value):
        raise ValueError(f"{name} must be a finite number.")
    # Enforce the minimum, including whether equality is permitted.
    if value < minimum or (strict and value == minimum):
        relation = 'greater than' if strict else 'at least'
        raise ValueError(f"{name} must be {relation} {minimum}.")
    # Enforce the upper bound when one has been supplied.
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}.")
    return value


# Validate each funding pool and require their total to match the starting budget.
def validate_allocations(starting_cash, dca, swing, reserve):
    number(starting_cash, 'starting_budget', strict=True)
    # Check DCA allocation, swing allocation, and reserve individually for nonnegative finite values.
    for name, value in [('dca.allocation', dca), ('swing.allocation', swing), ('reserve', reserve)]:
        number(value, name)
    # Reject allocations whose sum differs from the budget beyond the absolute tolerance.
    if not isclose(dca + swing + reserve, starting_cash, rel_tol=0, abs_tol=1e-8):
        raise ValueError('DCA allocation + swing allocation + reserve must equal starting_budget.')


# Validate the strategy mode, funding, trade sizes, execution rates, and model settings.
def validate_config(config):
    """Reject invalid JSON settings; do not silently coerce strings or booleans."""
    # Resolve a dotted configuration path and report missing sections or keys clearly.
    def get(path):
        item = config
        # Descend one dictionary level for each component in the dotted setting name.
        for key in path.split('.'):

            # Reject missing keys or a non-dictionary encountered before the setting is reached.
            if not isinstance(item, dict) or key not in item:
                raise ValueError(f'Missing configuration setting: {path}')
            
            item = item[key]
        return item
    
    mode = get("strategy_mode")

    # Allow only the three supported entry modes.
    if mode not in ("hybrid", "dca_only", "pause_entries"):
        raise ValueError(
            "strategy_mode must be hybrid, dca_only, or pause_entries."
        )

    # Check that the initial DCA, swing, and reserve allocations exhaust the budget.
    validate_allocations(get('starting_budget'), get('dca.allocation'),
                         get('swing.allocation'), get('reserve'))
    
    # Require positive amounts and multipliers for each listed trading or risk parameter.
    for path in ['dca.amount', 'swing.trade_amount', 'atr.multiplier',
                 'risk.reference_hourly_sigma', 'risk.minimum_swing_amount',
                 'exits.garch_trailing_multiplier']:
        
        number(get(path), path, strict = True)

    # Require the DCA drop threshold to be a positive fraction no greater than one.
    number(get('dca.drop_threshold'), 'dca.drop_threshold', strict = True, maximum = 1)

    # Validate the execution fee and slippage rates as nonnegative finite fractions.
    for path in ['execution.fee_rate', 'execution.slippage_rate']:
        value = number(get(path), path)

        # Reject execution rates of 100 percent or higher.
        if value >= 1:
            raise ValueError(f'{path} must be less than 1 (rates are fractions).')
        
    # Check each integer model setting against its own minimum allowed value.
    for path, minimum in [('atr.period', 1), ('garch.window_hours', 3),
                          ('garch.min_observed_returns', 1), ('garch.n_paths', 1),
                          ('garch.forecast_every_hours', 1), ('garch.seed', 0)]:
        value = get(path)

        # Reject non-integers, including Booleans, and values below the specified minimum.
        if type(value) is not int or value < minimum:
            raise ValueError(f'{path} must be an integer >= {minimum}.')
        
    # Require an hourly forecast schedule supported by this runner.
    if get('garch.forecast_every_hours') != 1:
        raise ValueError('This runner requires garch.forecast_every_hours = 1.')
    
    # Require enough price observations to support the requested number of observed returns.
    if get('garch.min_observed_returns') >= get('garch.window_hours'):
        raise ValueError('GARCH window must contain more prices than min_observed_returns.')
    
    # Accept only bullish or bearish crossover entry settings.
    if get('swing.crossover') not in ('bull', 'bear'):
        raise ValueError('swing.crossover must be "bull" or "bear".')
    
    # Check that each listed feature switch is explicitly Boolean.
    for path in ['risk.garch_sizing_enabled', 'exits.garch_trailing_enabled',
                 'exits.bearish_crossover_enabled']:
        # Reject numeric or string substitutes for true and false.
        if type(get(path)) is not bool:
            raise ValueError(f'{path} must be true or false.')
