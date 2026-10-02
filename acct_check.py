from portfolio import Portfolio

p = Portfolio(
    starting_cash=10000,
    dca_allocation=6000,
    swing_allocation=3000,
    reserve=1000,
    fee_rate=0.001
)

p.buy_active(1000, 100, "test-buy")
p.sell_active(100, "test-sell")

print("Swing cash:", p.swing_cash)
print("Reserve:", p.reserve_cash)
print("Portfolio value:", p.get_value(100))
print("Realized P/L:", p.realized_pnl)