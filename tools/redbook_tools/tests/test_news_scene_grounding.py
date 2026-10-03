from types import SimpleNamespace
import socket

import pytest

from src.workflow import create_post as w


REFINERY_TITLE = "乌克兰持续打击俄炼油厂"
REFINERY_FACTS = "乌克兰继续对俄罗斯炼油厂实施打击，不顾美国总统特朗普的反对。特朗普将相关袭击归咎为中期选举前美国燃料价格上涨的原因，但分析人士指出，其发动的伊朗战争才是油价上涨的主要原因。"
REFINERY_WRITER = "夜幕下，一座炼油厂的部分设施燃起大火并冒出浓烟，工业塔架与管道在火光中显现出轮廓，画面以受损的厂区为主体进行构图，远景为暗色天空。"
REFINERY_REWRITE = "画面中是一座大型炼油厂的多座工业储罐与蒸馏塔，厂区局部冒起黑烟，几道烟柱在阴沉天空下向上升腾，厂房外墙可见焦黑痕迹，远处地平线上火光隐约闪烁，整体构图以炼油设施为主体，烟与塔的轮廓构成紧张氛围。"
STREET_TITLE = "内塔尼亚胡面临下台选举"
STREET_FACTS = "以色列距一场可能令现任总理内塔尼亚胡下台的选举仅剩数周，耶路撒冷街头社会分歧明显。报道描述，耶路撒冷街头汇聚了世俗犹太人、极端正统派犹太人、宗教民族主义者、定居者、巴勒斯坦人以及从部署任务归来的士兵，不同群体立场不一。"
STREET_SCENE = "耶路撒冷一条繁忙的商业街道上，多名市民以不同装束交错经过，画面前景中一个侧影人物站在路边举手向过往行人示意，身后是现代店铺与传统石砌建筑交替排列的街面，远处的行人步履各异，街道地面散落着秋日阳光的光影，整体呈现城市日常与街头政治表达并存的瞬间。"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("offline scene grounding test")
    monkeypatch.setattr(socket.socket, "connect", deny)


def normalize(scene, title, facts, *, opinion="仍需观察。", audit=None):
    return w._normalize_daily_news_image_event(
        scene, title=title, body=f"内容：\n{facts}\n\n评价：\n{opinion}",
        picked=SimpleNamespace(title="Unrelated recommendation", content=scene),
        prompt_norm="国际新闻", audit=audit,
    )


@pytest.mark.parametrize("scene,title,facts", [
    (REFINERY_WRITER, REFINERY_TITLE, REFINERY_FACTS),
    (REFINERY_REWRITE, REFINERY_TITLE, REFINERY_FACTS),
    (STREET_SCENE, STREET_TITLE, STREET_FACTS),
])
def test_real_scenes_keep_complete_writer_facts_instead_of_short_title(scene, title, facts):
    audit = {"writer_value": scene}
    assert normalize(scene, title, facts, audit=audit) == scene
    assert audit["accepted"] is True
    assert audit["writer_value"] == audit["normalized"] == scene
    assert audit["version"] != "scene-anchor-v1"
    assert audit["supported_anchors"]
    assert audit["evidence_sentence"] in facts


@pytest.mark.parametrize("place", ["海港新区", "北部城区"])
def test_visual_grounding_is_not_specific_to_runtime_city(place):
    scene = f"{place}商业街道上，市民和行人交错经过，远处店铺轮廓简化为平涂色块，人物装束各异。"
    facts = f"报道描述，{place}街头聚集了来自不同社区的群体，居民讨论即将到来的选举。"
    assert normalize(scene, "当地选举仍存分歧", facts) == scene


def test_infrastructure_scene_is_not_specific_to_refinery_case():
    scene = "一座污水处理厂部分设施冒出浓烟，管道在火光中显现轮廓，远景为暗色天空。"
    assert normalize(scene, "设施遇袭调查展开", "当地污水处理厂遭到袭击，救援人员赶赴现场。") == scene


@pytest.mark.parametrize("facts,scene", [
    ("军方正在讨论对炼油厂实施打击的计划，尚未实施。", REFINERY_REWRITE),
    ("炼油厂未遭打击，也没有发生火灾。", REFINERY_WRITER),
    ("炼油厂今天完成设备例行检修，生产运行正常。", REFINERY_REWRITE),
    ("市政府计划在海港新区建设地铁，尚未开工。", "海港新区地铁已经通车，市民乘坐列车穿越新建车站。"),
    ("候选人表示希望在下一次选举中胜出，投票尚未举行。", "候选人已经赢得选举，站在台上庆祝胜选。"),
    ("炼油厂将被打击，目前尚无损毁报告。", REFINERY_REWRITE),
    ("炼油厂即将受到袭击。", REFINERY_WRITER),
])
def test_future_denied_or_unrelated_results_are_not_illustrated_as_accomplished(facts, scene):
    title = "相关计划及最新进展说明"
    audit = {}
    assert normalize(scene, title, facts, audit=audit) == ""
    assert audit["accepted"] is False
    assert audit["reason"] == "unsupported_scene_state"


@pytest.mark.parametrize("scene", [
    "耶路撒冷一条街道上，赛车手驾驶赛车争夺比赛冠军，观众挥手欢呼。",
    "炼油厂工人在草地上踢足球争夺冠军，背景为体育比赛观众席。",
    "市民在海港新区跳舞庆祝新地铁已经通车。",
    "耶路撒冷街道上，军人向市民开火，人群四散逃跑。",
])
def test_shared_entity_does_not_license_unrelated_actions(scene):
    facts = STREET_FACTS if "耶路撒冷" in scene else REFINERY_FACTS
    assert normalize(scene, "当天新闻进展", facts) == ""


def test_proposal_discussion_is_allowed_without_showing_completion():
    scene = "市政府官员讨论地铁建设计划，桌面摆放车站示意模型，人物与物件表面留白。"
    facts = "市政府官员讨论地铁建设计划，项目尚未开工。"
    assert normalize(scene, "市政府讨论地铁建设", facts) == scene


def test_unrelated_fact_in_opinion_cannot_supply_scene_anchor():
    assert normalize(STREET_SCENE, REFINERY_TITLE, REFINERY_FACTS, opinion=STREET_FACTS) == ""


def test_background_damage_to_other_subject_does_not_support_proposed_target():
    facts = "炼油厂计划成为打击目标，尚未实施。另一座厂房已经发生火灾。"
    assert normalize(REFINERY_REWRITE, REFINERY_TITLE, facts) == ""


def test_long_visual_details_do_not_dilute_fact_anchor_or_get_sliced():
    scene = STREET_SCENE + "建筑以柔和色块呈现，前景和远景层次分明，衣物及所有物体表面纯色留白。" * 4
    assert normalize(scene, STREET_TITLE, STREET_FACTS) == scene
