from fractions import Fraction

import pytest

from vacation_memories.render_config import RenderConfig
from vacation_memories.render_filters import (
    BlockItem,
    Geometry,
    audio_graph,
    block_graph,
    compose,
    even,
    fps_text,
    photo_segment_graph,
    seconds_text,
    video_segment_graph,
)

CONFIG = RenderConfig()
FPS = Fraction(30)


def test_even_rounding():
    assert [even(v) for v in (1, 3, 4.9, 5.1, 719.4)] == [2, 4, 4, 6, 720]


def test_text_formats():
    assert fps_text(Fraction(30)) == "30" and fps_text(Fraction(30000, 1001)) == "30000/1001"
    assert seconds_text(0.8) == "0.8" and seconds_text(2.0) == "2" and seconds_text(0.0) == "0"
    assert seconds_text(1 / 3) == "0.333333"


@pytest.mark.parametrize(
    "source, target, plain",
    [((1920, 1080), (1280, 720), True), ((1280, 720), (1920, 1080), True), ((1920, 1088), (1920, 1080), True),
     ((1080, 1920), (1920, 1080), False), ((1440, 1080), (1920, 1080), False), ((1000, 1000), (1920, 1080), False)],
)
def test_plain_scale_only_when_proportions_match(source, target, plain):
    assert Geometry(source, target).plain_scale is plain


def test_foreground_fits_inside_the_target_without_distortion():
    geometry = Geometry((1080, 1920), (1920, 1080))  # vertical source, horizontal output
    width, height = geometry.foreground
    assert height == 1080 and width == 608  # 1080 * 9/16, rounded to even
    assert abs(width / height - 1080 / 1920) < 0.002

    wide = Geometry((1920, 1080), (1080, 1080))  # horizontal source, square output
    assert wide.foreground == (1080, 608)


def test_a_small_source_is_scaled_up_to_fit():
    assert Geometry((160, 90), (1920, 1080)).foreground == (1920, 1080)
    assert Geometry((90, 160), (1920, 1080)).foreground == (608, 1080)


def test_crop_has_the_target_proportions_and_stays_inside_the_source():
    for source, target in [((1080, 1920), (1920, 1080)), ((1920, 1080), (1080, 1080)), ((1000, 1000), (1920, 1080))]:
        crop_w, crop_h = Geometry(source, target).crop
        assert crop_w <= source[0] and crop_h <= source[1]
        assert crop_w / crop_h == pytest.approx(target[0] / target[1], rel=0.01)


def test_compose_with_matching_proportions_is_a_single_scale():
    assert compose("in", "out", Geometry((1920, 1080), (1280, 720)), CONFIG, "v") == [
        "[in]scale=1280:720:flags=lanczos[out]"
    ]


def test_compose_otherwise_puts_the_source_on_a_blurred_background():
    statements = compose("in", "out", Geometry((1080, 1920), (1920, 1080)), CONFIG, "v")
    text = ";".join(statements)
    assert statements[0] == "[in]split=2[vsrc_bg][vsrc_fg]"
    assert "crop=1080:606" in text and "boxblur=" in text and "scale=1920:1080:flags=bilinear[vbg]" in text
    assert "scale=608:1080:flags=lanczos[vfg]" in text
    assert statements[-1] == "[vbg][vfg]overlay=x=656:y=0:format=yuv420[out]"


def test_the_blur_radius_stays_valid_for_small_frames():
    text = ";".join(compose("in", "out", Geometry((1080, 1920), (160, 90)), CONFIG, "v"))
    radius = int(text.split("luma_radius=")[1].split(":")[0])
    small_w = int(text.split("scale=")[2].split(":")[0])
    assert 1 <= radius <= small_w // 4


def test_video_graph_applies_timestamps_rate_and_composition():
    graph = video_segment_graph(
        stored_size=(1920, 1080), rotation=0, sample_aspect_ratio=Fraction(1), target=(1280, 720),
        fps=FPS, hdr_filter=None, config=CONFIG,
    )
    assert graph.startswith("[0:0]setpts=PTS-STARTPTS,fps=30,format=yuv420p[cfr]")
    assert graph.endswith("[fitted]format=yuv420p,setsar=1[v]")


def test_video_graph_reads_the_stream_that_was_probed():
    graph = video_segment_graph(
        stream_index=2, stored_size=(1920, 1080), rotation=0, sample_aspect_ratio=Fraction(1), target=(1280, 720),
        fps=FPS, hdr_filter=None, config=CONFIG,
    )
    assert graph.startswith("[0:2]setpts")


def test_video_graph_uses_the_rotated_size_for_the_geometry():
    rotated = video_segment_graph(
        stored_size=(1920, 1080), rotation=90, sample_aspect_ratio=Fraction(1), target=(1920, 1080),
        fps=FPS, hdr_filter=None, config=CONFIG,
    )
    assert "scale=608:1080" in rotated  # displayed as 1080x1920, so a pillarboxed foreground


def test_video_graph_squares_the_pixels_first():
    graph = video_segment_graph(
        stored_size=(240, 180), rotation=0, sample_aspect_ratio=Fraction(4, 3), target=(320, 180),
        fps=FPS, hdr_filter=None, config=CONFIG,
    )
    assert "scale=320:180:flags=bicubic,setsar=1" in graph


def test_video_graph_inserts_the_hdr_conversion_before_the_composition():
    graph = video_segment_graph(
        stored_size=(1920, 1080), rotation=0, sample_aspect_ratio=Fraction(1), target=(1280, 720),
        fps=FPS, hdr_filter="tonemap=hable", config=CONFIG,
    )
    assert graph.index("tonemap=hable") < graph.index("[cfr]")


def test_photo_graph_composes_on_an_oversized_canvas_then_zooms():
    graph = photo_segment_graph(photo_size=(1500, 2000), target=(1280, 720), fps=FPS, frames=90, zoom_in=True, config=CONFIG)
    assert "out_color_matrix=bt709" in graph  # RGB to BT.709 once, matching the tags of the output
    assert "scale=2560:1440:flags=bilinear" in graph  # the blurred background at 2x
    assert "d=90:s=1280x720:fps=30" in graph
    assert "z='1+0.06*on/89'" in graph


def test_photo_graph_zooms_out_for_alternate_photos_and_caps_the_canvas():
    graph = photo_segment_graph(photo_size=(1500, 2000), target=(3840, 2160), fps=FPS, frames=1, zoom_in=False, config=CONFIG)
    assert "z='1+0.06*(1-on/1)'" in graph
    assert "scale=4096:2304:flags=bilinear" in graph  # long side capped at 4096 (factor 1.07)


def test_block_graph_of_cuts_is_a_concat():
    graph = block_graph([BlockItem(30, 0), BlockItem(30, 0), BlockItem(30, 0)], FPS)
    assert graph == "[0:v][1:v][2:v]concat=n=3:v=1:a=0[joined];[joined]format=yuv420p,setsar=1[vout]"


def test_block_graph_of_dissolves_is_an_xfade_chain_with_exact_offsets():
    graph = block_graph([BlockItem(60, 0), BlockItem(60, 15), BlockItem(30, 9)], FPS)
    first, second, tail = graph.split(";")
    assert first == "[0:v][1:v]xfade=transition=fade:duration=0.5:offset=1.491667[r0x1]"  # (60-15-0.25)/30
    assert second == "[r0x1][2:v]xfade=transition=fade:duration=0.3:offset=3.191667[r0x2]"  # (105-9-0.25)/30: 105 frames are left after the first dissolve
    assert tail == "[r0x2]format=yuv420p,setsar=1[vout]"


def test_block_graph_never_feeds_a_concat_into_an_xfade():
    graph = block_graph([BlockItem(60, 0), BlockItem(60, 15), BlockItem(60, 0), BlockItem(60, 15)], FPS)
    assert graph.count("concat") == 1 and graph.index("concat") > graph.rindex("xfade")


def test_block_graph_fades_and_output_filters():
    graph = block_graph([BlockItem(150, 0)], FPS, fade_in=1.0, fade_out=2.0, total_frames=150, output_filters="format=nv12,hwupload")
    assert graph == "[0:v]format=yuv420p,setsar=1,fade=t=in:st=0:d=1,fade=t=out:st=3:d=2,format=nv12,hwupload[vout]"


def test_silent_audio_graph():
    graph = audio_graph(duration=5.5, sample_rate=48000, fade_in=1, fade_out=2, music_inputs=[], crossfade=0)
    assert graph == "anullsrc=channel_layout=stereo:sample_rate=48000,atrim=duration=5.5,asetpts=PTS-STARTPTS[aout]"


def test_music_audio_graph_trims_and_fades():
    graph = audio_graph(duration=10, sample_rate=48000, fade_in=1, fade_out=3, music_inputs=[2], crossfade=0)
    assert "[2:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[m2]" in graph
    assert graph.endswith("[m2]atrim=duration=10,asetpts=PTS-STARTPTS,afade=t=in:st=0:d=1,afade=t=out:st=7:d=3[aout]")


def test_looped_music_is_chained_with_crossfades():
    graph = audio_graph(duration=10, sample_rate=48000, fade_in=0, fade_out=0, music_inputs=[1, 2, 3], crossfade=1.5)
    assert "[m1][m2]acrossfade=d=1.5:c1=tri:c2=tri[loop1]" in graph
    assert "[loop1][m3]acrossfade=d=1.5:c1=tri:c2=tri[loop2]" in graph
    assert graph.endswith("[loop2]atrim=duration=10,asetpts=PTS-STARTPTS[aout]")


def test_fades_never_exceed_half_of_a_short_video():
    graph = audio_graph(duration=2, sample_rate=48000, fade_in=5, fade_out=5, music_inputs=[1], crossfade=0)
    assert "afade=t=in:st=0:d=1" in graph and "afade=t=out:st=1:d=1" in graph
