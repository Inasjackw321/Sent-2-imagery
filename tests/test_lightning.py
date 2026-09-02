"""Tests for the lightning layer's catalogue reading.

The layer identifiers are not written into the app: it asks GIBS what it is
serving and uses what comes back. That makes this parser the whole of the
join, and the one place a mistake stops the layer working -- so it is what is
tested, against documents shaped like the real one rather than against the
service, which cannot be reached from here.

The previous version of this file tested a websocket protocol that could not
be exercised either, and the layer shipped broken. The lesson taken is not
"test harder" but "depend on less": a WMTS catalogue is a document, and a
document can be checked.
"""

from __future__ import annotations

import pytest

from backend import lightning

WMTS = "http://www.opengis.net/wmts/1.0"
OWS = "http://www.opengis.net/ows/1.1"


def capabilities(*layers: str) -> str:
    return (
        f'<Capabilities xmlns="{WMTS}" xmlns:ows="{OWS}">'
        f"<Contents>{''.join(layers)}</Contents></Capabilities>"
    )


def layer(ident: str, *, title: str | None = None, matrix: str = "GoogleMapsCompatible_Level7",
          fmt: str = "image/png", default: str | None = "2026-09-01T12:00:00Z",
          values: tuple[str, ...] = ()) -> str:
    time_dim = ""
    if default is not None:
        vals = "".join(f"<Value>{v}</Value>" for v in values)
        time_dim = (f"<Dimension><ows:Identifier>Time</ows:Identifier>"
                    f"<Default>{default}</Default>{vals}</Dimension>")
    return (
        f"<Layer>"
        f"<ows:Title>{title or ident}</ows:Title>"
        f"<ows:Identifier>{ident}</ows:Identifier>"
        f"{time_dim}"
        f"<TileMatrixSetLink><TileMatrixSet>{matrix}</TileMatrixSet></TileMatrixSetLink>"
        f"<Format>{fmt}</Format>"
        f"</Layer>"
    )


class TestParse:
    def test_it_finds_the_glm_layers_and_ignores_the_rest(self):
        xml = capabilities(
            layer("MODIS_Terra_CorrectedReflectance_TrueColor"),
            layer("GOES-East_GLM_Flash_Extent_Density"),
            layer("VIIRS_SNPP_Thermal_Anomalies"),
            layer("GOES-West_GLM_Flash_Extent_Density"),
        )
        got = lightning.parse_layers(xml)
        assert [entry["id"] for entry in got] == [
            "GOES-East_GLM_Flash_Extent_Density",
            "GOES-West_GLM_Flash_Extent_Density",
        ]

    def test_it_reads_what_the_tile_url_needs(self):
        got = lightning.parse_layers(capabilities(layer(
            "GOES-East_GLM_Flash_Extent_Density_5min",
            title="Flash Extent Density (5 min)",
            matrix="GoogleMapsCompatible_Level6", fmt="image/png",
            default="2026-09-01T12:05:00Z",
            values=("2026-09-01T00:00:00Z/2026-09-01T23:55:00Z/PT5M",))))
        assert len(got) == 1
        entry = got[0]
        assert entry["title"] == "Flash Extent Density (5 min)"
        assert entry["matrix"] == "GoogleMapsCompatible_Level6"
        assert entry["format"] == "png"
        assert entry["time_default"] == "2026-09-01T12:05:00Z"
        assert entry["time_values"] == [
            "2026-09-01T00:00:00Z/2026-09-01T23:55:00Z/PT5M"]

    def test_it_tells_the_two_satellites_apart(self):
        got = lightning.parse_layers(capabilities(
            layer("GOES-East_GLM_Flash_Extent_Density"),
            layer("GOES-West_GLM_Flash_Extent_Density"),
            layer("Some_Other_GLM_Flash_Product"),
        ))
        assert {e["id"]: e["satellite"] for e in got} == {
            "GOES-East_GLM_Flash_Extent_Density": "east",
            "GOES-West_GLM_Flash_Extent_Density": "west",
            "Some_Other_GLM_Flash_Product": None,
        }

    def test_a_jpeg_layer_comes_back_with_the_extension_leaflet_wants(self):
        got = lightning.parse_layers(capabilities(
            layer("GOES-East_GLM_Flash_Density", fmt="image/jpeg")))
        assert got[0]["format"] == "jpg"

    def test_a_layer_with_no_time_dimension_is_still_usable(self):
        got = lightning.parse_layers(capabilities(
            layer("GOES-East_GLM_Flash_Extent_Density", default=None)))
        assert got[0]["time_default"] is None
        assert got[0]["time_values"] == []

    def test_no_glm_layers_is_an_empty_list_not_an_error(self):
        # If NASA stops publishing these, the layer must say so rather than
        # throw -- an empty picker with an explanation beats a stack trace.
        got = lightning.parse_layers(capabilities(
            layer("MODIS_Terra_CorrectedReflectance_TrueColor")))
        assert got == []

    def test_rubbish_is_refused_clearly(self):
        with pytest.raises(lightning.LightningError, match="would not parse"):
            lightning.parse_layers("<not xml")

    def test_an_empty_document_finds_nothing(self):
        assert lightning.parse_layers(capabilities()) == []

    def test_the_matching_is_not_fooled_by_a_near_miss(self):
        # "flash" alone is a flood product; "glm" alone could be anything.
        got = lightning.parse_layers(capabilities(
            layer("MODIS_Flood_Flash_Extent"),
            layer("GLM_Something_Else"),
        ))
        assert got == []


class TestDemo:
    def test_it_answers_in_the_same_shape_as_the_real_thing(self):
        got = lightning.demo()
        assert set(got) == {"layers", "template", "attribution", "coverage"}
        for entry in got["layers"]:
            assert set(entry) >= {"id", "title", "matrix", "format", "satellite"}

    def test_the_template_has_every_placeholder_the_page_fills_in(self):
        for token in ("{layer}", "{time}", "{matrix}", "{z}", "{y}", "{x}", "{fmt}"):
            assert token in lightning.demo()["template"], token

    def test_the_coverage_note_names_what_is_missing(self):
        # The whole point of it: a blank map over Europe has to explain itself.
        note = lightning.demo()["coverage"].lower()
        assert "europe" in note
        assert "goes" in note
