import pytest

from app.config import FiltersCfg, SystemFilterCfg, load_yaml_config
from app.config import DATA_DIR
from app.filters import BIOS, EXCLUDE, INCLUDE, RuleSet


@pytest.fixture(scope="module")
def rules() -> RuleSet:
    cfg = load_yaml_config((DATA_DIR / "config.default.yaml").read_text())
    return RuleSet(cfg.filters)


@pytest.mark.parametrize("name,expected", [
    ("Tetris (World) (Rev 1).gb", INCLUDE),
    ("Pokemon - Yellow Version (USA, Europe) (CGB+SGB Enhanced).gb", INCLUDE),
    ("10-Pin Bowling (USA) (Proto).gb", EXCLUDE),
    ("Aero Blasters (Japan) (En).pce", INCLUDE),
    ("Army Men - Sarge_s Heroes 2 (U) [!].z64", INCLUDE),
    ("101 Dalmatas (USA) (Pirate).sfc", EXCLUDE),
    ("007 - Blood Stone (Canada) (Fr).zip", EXCLUDE),
    ("[BIOS] Atari Lynx (World).lyx", BIOS),
    ("GB/[BIOS] Nintendo Game Boy Boot ROM (World) (Rev 1).gb", BIOS),
])
def test_real_world_names(rules, name, expected):
    assert rules.decide(name).category == expected


@pytest.mark.parametrize("name,expected", [
    ("Game (Japan, USA).sfc", INCLUDE),
    ("Game (Europe).sfc", EXCLUDE),
    ("Game (Japan) (En,Ja).sfc", INCLUDE),
    ("Game (Japan) (Ja).sfc", EXCLUDE),
    ("Game (USA) (Beta 2).sfc", EXCLUDE),
    ("Game (JUE) [!].smc", INCLUDE),
    ("Game (USA) (Program).sfc", EXCLUDE),
])
def test_more_rules(rules, name, expected):
    assert rules.decide(name).category == expected


def test_folder_path_is_ignored(rules):
    # A "(USA)" folder must not make a European file match, nor "(Proto)" folders exclude.
    assert rules.decide("Games (USA)/Game (Europe).gb").category == EXCLUDE
    assert rules.decide("(Proto) stuff/Game (USA).gb").category == INCLUDE


def test_rule_reason_is_reported(rules):
    d = rules.decide("10-Pin Bowling (USA) (Proto).gb")
    assert "Proto" in d.rule


def test_per_system_allow_deny_and_override():
    cfg = load_yaml_config((DATA_DIR / "config.default.yaml").read_text())
    cfg.filters.systems["snes"] = SystemFilterCfg(
        allow=["Terranigma (Europe)"], deny=["re:^Bad Game"], exclude=[])
    r = RuleSet(cfg.filters, "snes")
    assert r.decide("Terranigma (Europe).sfc").category == INCLUDE
    assert r.decide("Bad Game (USA).sfc").category == EXCLUDE
    assert r.decide("Bad Game (USA).sfc").rule.startswith("deny-list")
    # exclude overridden to empty: protos allowed for snes only
    assert r.decide("Game (USA) (Proto).sfc").category == INCLUDE
    assert RuleSet(cfg.filters, "gb").decide("Game (USA) (Proto).gb").category == EXCLUDE
    # BIOS beats allow-list
    cfg.filters.systems["snes"].allow.append("[BIOS]")
    assert RuleSet(cfg.filters, "snes").decide("[BIOS] SNES (USA).sfc").category == BIOS
