import pytest

from agent_ops.routing_posthoc import exact_mcnemar, holm_adjust, macro_f1, paired_analysis


def test_exact_mcnemar_known_binomial_cases():
    assert exact_mcnemar(0,0)==1
    assert exact_mcnemar(5,5)==1
    assert exact_mcnemar(10,0)==pytest.approx(2/1024)
    assert exact_mcnemar(0,10)==exact_mcnemar(10,0)
    with pytest.raises(ValueError):exact_mcnemar(-1,2)


def test_holm_known_multiple_testing_example():
    assert holm_adjust([.01,.04,.03])==pytest.approx([.03,.06,.06])


def test_excluding_gold_human_does_not_drop_predicted_human_errors():
    assert macro_f1(['order_status','return_request','clarify'],['human','return_request','clarify'],('order_status','return_request','clarify'))==pytest.approx(2/3)


def test_paired_bootstrap_identical_versions_zero_difference_and_reproducibility():
    rows=[{'label':label,'rule':label,'model':label,'hybrid':label} for label in ['order_status','return_request','clarify','human']]
    report=paired_analysis(rows,replicates=100,seed=4,exclude_gold_human=True)
    assert report==paired_analysis(rows,replicates=100,seed=4,exclude_gold_human=True)
    assert report['n']==3
    assert all(v['macro_f1']==1 and v['ci95']==[1,1] for v in report['versions'].values())
    assert all(p['paired_bootstrap_ci95']==[0,0] and p['mcnemar_exact_two_sided_p']==1 for p in report['pairs'])
