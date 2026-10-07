"""Validate settings before downloading data or changing any balances."""
from math import isclose, isfinite
from numbers import Real


def number(value, name, *, minimum=0, strict=False, maximum=None):
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value):
        raise ValueError(f"{name} must be a finite number.")
    if value < minimum or (strict and value == minimum):
        relation = 'greater than' if strict else 'at least'
        raise ValueError(f"{name} must be {relation} {minimum}.")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}.")
    return value


def validate_allocations(starting_cash, dca, swing, reserve):
    number(starting_cash, 'starting_budget', strict=True)
    for name, value in [('dca.allocation', dca), ('swing.allocation', swing), ('reserve', reserve)]:
        number(value, name)
    if not isclose(dca + swing + reserve, starting_cash, rel_tol=0, abs_tol=1e-8):
        raise ValueError('DCA allocation + swing allocation + reserve must equal starting_budget.')


def validate_config(config):
    """Reject invalid JSON settings; do not silently coerce strings or booleans."""
    def get(path):
        item = config
        for key in path.split('.'):
            if not isinstance(item, dict) or key not in item:
                raise ValueError(f'Missing configuration setting: {path}')
            item = item[key]
        return item

    validate_allocations(get('starting_budget'), get('dca.allocation'),
                         get('swing.allocation'), get('reserve'))
    for path in ['dca.amount', 'swing.trade_amount', 'atr.multiplier',
                 'risk.reference_hourly_sigma', 'risk.minimum_swing_amount',
                 'exits.garch_trailing_multiplier']:
        number(get(path), path, strict=True)
    number(get('dca.drop_threshold'), 'dca.drop_threshold', strict=True, maximum=1)
    for path in ['execution.fee_rate', 'execution.slippage_rate']:
        value = number(get(path), path)
        if value >= 1:
            raise ValueError(f'{path} must be less than 1 (rates are fractions).')
    for path, minimum in [('atr.period', 1), ('garch.window_hours', 3),
                          ('garch.min_observed_returns', 1), ('garch.n_paths', 1),
                          ('garch.forecast_every_hours', 1), ('garch.seed', 0)]:
        value = get(path)
        if type(value) is not int or value < minimum:
            raise ValueError(f'{path} must be an integer >= {minimum}.')
    if get('garch.forecast_every_hours') != 1:
        raise ValueError('This runner requires garch.forecast_every_hours = 1.')
    if get('garch.min_observed_returns') >= get('garch.window_hours'):
        raise ValueError('GARCH window must contain more prices than min_observed_returns.')
    if get('swing.crossover') not in ('bull', 'bear'):
        raise ValueError('swing.crossover must be "bull" or "bear".')
    for path in ['risk.garch_sizing_enabled', 'exits.garch_trailing_enabled',
                 'exits.bearish_crossover_enabled']:
        if type(get(path)) is not bool:
            raise ValueError(f'{path} must be true or false.')
