from __future__ import annotations

from videasy.utils.titles import normalize_title


def test_normalize_title_strips_lang_tags_season_and_year():
    assert normalize_title("Call Me Dad [English]") == "call me dad"
    assert normalize_title("Exhuma [Indonesian]") == "exhuma"
    assert normalize_title("Parasite [Hindi]") == "parasite"
    assert normalize_title("Avatar: The Last Airbender S1-S2") == "avatar the last airbender"
    assert normalize_title("Avatar: The Last Airbender Season 1") == "avatar the last airbender"
    assert normalize_title("Mission: Impossible (2025)") == "mission impossible"
    assert normalize_title("  Thunderbolts*  ") == "thunderbolts"


def test_normalize_makes_localized_titles_comparable():
    """Normalization is what allows a plain == to match localized variants."""
    assert normalize_title("How to Train Your Dragon") == normalize_title("How to Train Your Dragon [Indonesian]")
    assert normalize_title("Andai Ibu Tidak Menikah dengan Ayah") == normalize_title("Andai Ibu Tidak Menikah Dengan Ayah")
    assert normalize_title("Thunderbolts*") == normalize_title("Thunderbolts")
    # Korean title, same script, unaffected
    assert normalize_title("폭군") == normalize_title("폭군")
