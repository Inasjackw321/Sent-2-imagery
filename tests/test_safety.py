"""What this app refuses to do for a caller it has never met.

It listens on loopback and it is a browser away from every website the
operator has open. That is the threat model, and it is not hypothetical: a
page on any site can send a request to 127.0.0.1. What stops that being
interesting is the same-origin policy (so the page cannot read the answer)
and these checks (so the request cannot do anything worth doing).

Three things are tested here, each of which was actually open:

  A SCENE ARRIVES IN THE REQUEST BODY. The page hands back the scene it got
  from the catalogue, so the asset addresses in it are whatever the caller
  says. They end up at rasterio.open, and GDAL opens far more than an HTTPS
  URL: a path on this disk, a /vsicurl/ address, a driver nobody thought
  about. Unchecked, a render reads local files and makes requests from
  inside this process's network -- a metadata service, a database on
  loopback -- which the caller cannot reach and this process can.

  A DOWNLOAD'S FILENAME IS BUILT FROM THAT SCENE. It goes into a response
  header, so a quote in it is a second filename and a newline is a second
  header.

  THE WMS PROXY FORWARDS WHAT IT IS GIVEN. A WMS `sld` parameter is a URL
  the upstream server fetches, which would make this proxy the first step of
  somebody else's request.
"""

from __future__ import annotations

import pytest

from backend import app as A, copernicus, raster


AOI = {"type": "Polygon", "coordinates":
       [[[30.0, 50.0], [30.1, 50.0], [30.1, 50.1], [30.0, 50.1], [30.0, 50.0]]]}


def scene_with(href: str) -> dict:
    return {"id": "made-up", "date": "2026-01-01", "satellite": "sentinel-2",
            "source": "earth-search",
            "assets": {"red": href, "green": href, "blue": href}}


# ── Where a band may be read from ──────────────────────────────


class TestAssetAddresses:
    def test_an_ordinary_catalogue_address_is_fine(self):
        assert raster.safe_href(
            "https://sentinel-cogs.s3.us-west-2.amazonaws.com/x/B04.tif")

    def test_a_signed_address_is_still_fine(self):
        assert raster.safe_href(
            "https://x.blob.core.windows.net/y/B04.tif?st=2026&sig=abc")

    @pytest.mark.parametrize("href", [
        "/etc/passwd",
        "/home/someone/secret.tif",
        "file:///etc/passwd",
        "C:\\Windows\\win.ini",
        "/vsicurl/http://169.254.169.254/latest/meta-data/",
        "/vsizip//etc/backup.zip/inside.tif",
        "/vsis3/somebucket/key.tif",
        "http://example.com/x.tif",          # plain http: no transport security
        "ftp://example.com/x.tif",
        "",
        None,
    ])
    def test_anything_that_is_not_an_https_address_is_refused(self, href):
        with pytest.raises(raster.BandReadError):
            raster.safe_href(href)

    @pytest.mark.parametrize("host", [
        "localhost", "127.0.0.1", "127.1.1.1", "0.0.0.0",
        "169.254.169.254",            # the cloud metadata service
        "10.0.0.5", "192.168.1.1", "172.16.0.1", "172.31.255.255",
        "metadata.google.internal",
        "[::1]",
    ])
    def test_no_address_inside_this_machine_or_network(self, host):
        with pytest.raises(raster.BandReadError, match="private network|machine"):
            raster.safe_href(f"https://{host}/x.tif")

    def test_a_public_address_that_merely_looks_private_is_fine(self):
        """172.15 and 172.32 are not in the private range, and a host called
        something like "10.tiles.example.com" is a name, not an address."""
        assert raster.safe_href("https://172.15.0.1/x.tif")
        assert raster.safe_href("https://172.32.0.1/x.tif")
        assert raster.safe_href("https://10tiles.example.com/x.tif")

    def test_credentials_do_not_smuggle_a_host_past_the_check(self):
        """https://sentinel-cogs.example.com@127.0.0.1/ is a request to
        127.0.0.1, whatever it reads like."""
        with pytest.raises(raster.BandReadError):
            raster.safe_href("https://sentinel-cogs.example.com@127.0.0.1/x.tif")

    def test_the_refusal_says_what_was_refused_and_why(self):
        with pytest.raises(raster.BandReadError) as caught:
            raster.safe_href("/etc/passwd")
        said = str(caught.value)
        assert "https" in said
        assert "/etc/passwd" in said


class TestWalkingAScene:
    def test_a_bad_asset_anywhere_in_a_scene_is_found(self):
        with pytest.raises(raster.BandReadError):
            raster.scene_is_safe(scene_with("/etc/passwd"))

    def test_a_good_scene_passes(self):
        raster.scene_is_safe(scene_with("https://example.com/x.tif"))

    def test_an_asset_given_as_a_stac_object_is_read_into(self):
        """Both shapes turn up in a body: a bare address, and the object the
        catalogue publishes with the address inside it."""
        with pytest.raises(raster.BandReadError):
            raster.scene_is_safe({"assets": {"red": {"href": "/etc/passwd"}}})

    def test_every_scene_in_a_list_is_checked_not_just_the_first(self):
        body = {"scenes": [scene_with("https://example.com/x.tif"),
                           scene_with("/etc/passwd")]}
        with pytest.raises(raster.BandReadError):
            raster.scenes_are_safe(body)

    def test_the_single_scene_is_checked_too(self):
        with pytest.raises(raster.BandReadError):
            raster.scenes_are_safe({"scene": scene_with("/etc/passwd")})

    def test_a_body_with_no_scene_at_all_is_not_a_failure(self):
        raster.scenes_are_safe({})
        raster.scenes_are_safe({"scenes": []})
        raster.scenes_are_safe(None)

    def test_rubbish_where_a_scene_should_be_does_not_raise_here(self):
        """It is not this function's job to validate shapes -- only to refuse
        addresses. A string where a scene should be fails later, clearly."""
        raster.scenes_are_safe({"scene": "nonsense"})
        raster.scenes_are_safe({"scenes": ["nonsense", 7]})


class TestTheseChecksAreActuallyWiredUp:
    """The check is worth nothing if a route forgets to call it."""

    def test_render_refuses_a_local_file(self, client):
        r = client.post("/api/render", json={
            "aoi": AOI, "scene": scene_with("/etc/passwd"),
            "preset": "true_color", "size": 256})
        assert r.status_code == 400
        assert "https" in r.json()["detail"]

    def test_render_refuses_the_metadata_service(self, client):
        r = client.post("/api/render", json={
            "aoi": AOI,
            "scene": scene_with("https://169.254.169.254/latest/meta-data/"),
            "preset": "true_color", "size": 256})
        assert r.status_code == 400

    def test_probe_refuses_one_too(self, client):
        r = client.post("/api/probe", json={
            "aoi": AOI, "scene": scene_with("/etc/passwd"),
            "lon": 30.05, "lat": 50.05})
        assert r.status_code == 400

    def test_animate_refuses_one_too(self, client):
        r = client.post("/api/animate", json={
            "aoi": AOI,
            "scenes": [scene_with("https://example.com/a.tif"),
                       scene_with("/etc/passwd")],
            "preset": "true_color", "size": 256})
        assert r.status_code == 400


# ── The download filename ──────────────────────────────────────


class TestDownloadFilenames:
    def test_an_ordinary_stem_is_left_alone(self):
        assert A.filename_stem("sentinel2_2026-01-01_true_color") \
            == "sentinel2_2026-01-01_true_color"

    def test_a_quote_cannot_start_a_second_filename(self):
        assert '"' not in A.filename_stem('a"; filename="evil.exe')

    def test_a_newline_cannot_start_a_second_header(self):
        made = A.filename_stem("a\r\nX-Injected: yes")
        assert "\r" not in made and "\n" not in made

    def test_a_semicolon_and_a_space_go_too(self):
        made = A.filename_stem("a; b c")
        assert ";" not in made and " " not in made

    def test_a_path_cannot_be_smuggled_into_the_name(self):
        assert "/" not in A.filename_stem("../../etc/passwd")
        assert "\\" not in A.filename_stem("..\\..\\windows\\win.ini")

    def test_a_stem_of_nothing_useful_still_gives_a_filename(self):
        assert A.filename_stem('"""') == "imagery"
        assert A.filename_stem("") == "imagery"

    def test_a_very_long_stem_is_cut(self):
        assert len(A.filename_stem("x" * 500)) <= 120

    def test_the_download_header_is_actually_built_through_it(self):
        """The sanitiser is worth nothing if the header is built from the raw
        stem beside it -- which is how it was before."""
        made = A._render_response(
            {"bytes": b"x", "media_type": "image/png",
             "meta": {"satellite": "sentinel-2", "preset": "true_color",
                      "scene": {"date": "2026-01-01"}}},
            True, 'sentinel2_a"; filename="evil.exe_true_color')
        said = made.headers["content-disposition"]
        assert said.startswith('attachment; filename="') and said.endswith('"')
        # Whatever is between the quotes is one filename, with nothing in it
        # that could end the quoting and start a second one.
        inside = said[len('attachment; filename="'):-1]
        assert '"' not in inside and ";" not in inside, said

    def test_a_crafted_scene_date_cannot_reach_the_header(self):
        """The stem is built from the scene, and the scene came from the
        caller. This is the path that carries it."""
        made = A._render_response(
            {"bytes": b"x", "media_type": "image/png",
             "meta": {"satellite": "sentinel-2", "preset": "true_color",
                      "scene": {"date": "a\r\nX-Injected: yes"}}},
            True, 'sentinel2_a\r\nX-Injected: yes_true_color')
        said = made.headers["content-disposition"]
        assert "\r" not in said and "\n" not in said
        assert "X-Injected" not in made.headers

    def test_unicode_is_dropped_rather_than_passed_through(self):
        """A header is latin-1 on the wire; a Cyrillic place name in a
        filename is a 500 rather than a download."""
        made = A.filename_stem("Запоріжжя_2026")
        assert made.isascii()
        assert "2026" in made


# ── The tile proxy ─────────────────────────────────────────────


class TestTheProxyForwardsLittle:
    def test_an_sld_url_is_not_forwarded(self):
        """`sld` is fetched by the upstream server, which would make this
        proxy the first step of somebody else's request."""
        got = copernicus.tile_params(
            {"layers": "x", "sld": "http://169.254.169.254/"})
        assert "sld" not in got

    def test_an_inline_sld_document_is_not_forwarded_either(self):
        got = copernicus.tile_params(
            {"layers": "x", "sld_body": "<StyledLayerDescriptor/>"})
        assert "sld_body" not in got

    def test_the_parameters_a_map_actually_needs_still_are(self):
        got = copernicus.tile_params({
            "LAYERS": "mtg_fd:rgb", "BBOX": "1,2,3,4", "WIDTH": "256",
            "HEIGHT": "256", "CRS": "EPSG:3857", "TIME": "2026-01-01T00:00Z",
            "FORMAT": "image/png", "TRANSPARENT": "true"})
        assert set(got) == {"layers", "bbox", "width", "height", "crs",
                            "time", "format", "transparent"}

    def test_nothing_unrecognised_is_forwarded(self):
        got = copernicus.tile_params({"layers": "x", "map": "/etc/mapfile",
                                      "callback": "alert(1)"})
        assert set(got) == {"layers"}

    def test_the_proxy_only_ever_talks_to_one_host(self):
        """It takes no URL from its caller: the host is fixed in the source."""
        from backend import mtg
        assert mtg.WMS.startswith("https://view.eumetsat.int")


# ── The policy in front of all of it ───────────────────────────


class TestCrossOrigin:
    def test_no_origin_is_allowed_by_default(self):
        """A wildcard here would let a page on any site read every answer
        this loopback server gives."""
        assert A.CORS_ORIGINS == []

    def test_a_random_site_gets_no_permission_header(self, client):
        r = client.get("/api/config", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in {
            k.lower() for k in r.headers}

    def test_the_page_is_served_from_this_same_origin(self, client):
        """Which is why no CORS header is needed at all."""
        assert client.get("/").status_code == 200

    def test_the_answer_still_arrives(self, client):
        """Refusing the header is not refusing the request: this has to be
        the browser's rule, not a broken API."""
        assert client.get("/api/config").status_code == 200


class TestTheHeadersEveryAnswerCarries:
    def test_a_page_cannot_be_framed_by_another_site(self, client):
        assert "frame-ancestors 'none'" in A.CSP

    def test_nothing_may_be_guessed_at_by_content_type(self, client):
        assert client.get("/").headers["x-content-type-options"] == "nosniff"

    def test_no_referrer_leaves_this_app(self, client):
        assert client.get("/").headers["referrer-policy"] == "no-referrer"

    def test_the_policy_is_on_the_api_as_well_as_the_page(self, client):
        assert "content-security-policy" in {
            k.lower() for k in client.get("/api/config").headers}
