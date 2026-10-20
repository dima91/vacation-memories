from render_fakes import RUNNER

from vacation_memories.encoders import (
    SOFTWARE_ENCODER,
    choose_assembly_encoder,
    fps_mode_args,
    hardware_candidates,
    software_encoder,
    thread_args,
)
from vacation_memories.render_config import RenderConfig
from vacation_memories.runtime_profile import DetectionEnv, detect_ffmpeg

CONFIG = RenderConfig()
HW = ("h264_nvenc", "h264_omx", "h264_qsv", "h264_v4l2m2m", "h264_vaapi")


def test_fps_mode_depends_on_the_ffmpeg_version():
    assert fps_mode_args("5.1.9-0+deb12u1") == ["-fps_mode", "cfr"]
    assert fps_mode_args("6.0") == ["-fps_mode", "cfr"]
    assert fps_mode_args("4.2.7-0ubuntu0.1") == ["-vsync", "cfr"]
    assert fps_mode_args("5.0.3") == ["-vsync", "cfr"]
    assert fps_mode_args("n7.1") == ["-fps_mode", "cfr"]
    assert fps_mode_args("N-12345-gabcdef") == ["-fps_mode", "cfr"]  # a git build: assume it is recent
    assert fps_mode_args(None) == ["-fps_mode", "cfr"]


def test_software_encoder_arguments():
    encoder = software_encoder("veryfast", 16)
    assert encoder.is_software and encoder.name == SOFTWARE_ENCODER
    args = encoder.codec_args
    assert args[args.index("-preset") + 1] == "veryfast" and args[args.index("-crf") + 1] == "16"
    assert args[args.index("-pix_fmt") + 1] == "yuv420p"
    assert args[args.index("-colorspace") + 1] == "bt709" and "-bf" not in args


def test_conservative_encoder_turns_off_b_frames_and_limits_the_gop():
    args = software_encoder("veryfast", 16, conservative=True, gop=30).codec_args
    assert args[args.index("-bf") + 1] == "0" and args[args.index("-g") + 1] == "30"


def test_hardware_candidates():
    assert hardware_candidates("auto", HW) == ["h264_vaapi", "h264_qsv", "h264_nvenc"]
    assert hardware_candidates("vaapi", HW) == ["h264_vaapi"]
    assert hardware_candidates("qsv", ("h264_vaapi",)) == []


def test_default_is_software_with_no_reason_to_explain():
    encoder, reason = choose_assembly_encoder("none", HW, RUNNER, CONFIG, preview=False)
    assert encoder.is_software and reason is None


def test_preview_uses_the_fast_preset():
    encoder, _ = choose_assembly_encoder("none", HW, RUNNER, CONFIG, preview=True)
    assert encoder.codec_args[encoder.codec_args.index("-preset") + 1] == CONFIG.preview_preset


def test_no_matching_encoder_falls_back_to_software():
    encoder, reason = choose_assembly_encoder("vaapi", ("h264_nvenc",), RUNNER, CONFIG, preview=False)
    assert encoder.is_software and "no hardware H.264 encoder matches --hwaccel vaapi" in reason


def test_hardware_that_does_not_work_falls_back_to_software():
    # The development container has no GPU: FFmpeg lists the encoders but they cannot be used.
    info = detect_ffmpeg(DetectionEnv(), lambda message: None)
    assert "h264_vaapi" in info.hw_encoders
    encoder, reason = choose_assembly_encoder("auto", info.hw_encoders, RUNNER, CONFIG, preview=False)
    assert encoder.is_software
    assert reason.startswith("hardware encoding does not work here (") and "h264_vaapi" in reason


def test_thread_arguments():
    assert thread_args(None) == ([], [], [])
    assert thread_args(3) == (["-threads", "3"], ["-filter_complex_threads", "3"], ["-threads", "3"])
