import importlib.util
from pathlib import Path
import pytest

path=Path(__file__).resolve().parents[1]/"scripts"/"benchmark_news_pipeline.py"
spec=importlib.util.spec_from_file_location("news_benchmark",path)
benchmark=importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def test_fixture_modes_keep_quality_and_reduce_full_rewrites():
    legacy=benchmark.replay("legacy",count=2)
    staged=benchmark.replay("staged_v2",count=2)
    assert all(r["status"]=="success" for r in legacy["results"]+staged["results"]),(legacy,staged)
    assert [r["content"] for r in legacy["results"]]==[r["content"] for r in staged["results"]]
    assert legacy["calls"]["writer"]==3
    assert staged["calls"]["writer"]==2
    assert staged["calls"]["patch"]==1
    assert staged["calls"]["response_chars"]<legacy["calls"]["response_chars"]


def test_live_mode_is_not_implicitly_authorized(tmp_path):
    with pytest.raises(SystemExit):
        benchmark.main(["--live","--output",str(tmp_path)])
