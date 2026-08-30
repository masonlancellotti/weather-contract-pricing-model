from backtest.fees import ConservativeFixedFeeModel, NoFeeModel, PercentageFeeModel


def test_conservative_fixed_fee_scales_with_quantity():
    assert ConservativeFixedFeeModel(per_contract_cents=1.25).fee_cents(40, 3) == 3.75


def test_percentage_fee_has_minimum():
    model = PercentageFeeModel(rate=0.02, minimum_cents=0.5)
    assert model.fee_cents(10, 1) == 0.5
    assert model.fee_cents(80, 2) == 3.2


def test_no_fee_only_for_comparison():
    assert NoFeeModel().fee_cents(80, 99) == 0.0
