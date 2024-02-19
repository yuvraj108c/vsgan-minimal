import sys
import os

sys.path.append("/workspace/tensorrt/")
import vapoursynth as vs

core = vs.core
vs_api_below4 = vs.__api_version__.api_major < 4
core.num_threads = 8

core.std.LoadPlugin(path="/usr/local/lib/libvstrt.so")


def metrics_func(clip):
    offs1 = core.std.BlankClip(clip, length=1) + clip[:-1]
    offs1 = core.std.CopyFrameProps(offs1, clip)
    return core.vmaf.Metric(clip, offs1, 2)


def inference_clip(video_path="", clip=None):
    clip = core.bs.VideoSource(source=video_path)

    # ssim
    clip = vs.core.resize.Bicubic(clip, format=vs.RGBH, matrix_in_s="709")

    upscaled = core.trt.Model(
        clip,
        engine_path="/workspace/tensorrt/2x_AnimeJaNai_V2_Compact_36k_op18_fp16_clamp.engine",
        num_streams=2,
    )

    # ssim
    upscaled_metrics = vs.core.resize.Bicubic(
        clip, width=224, height=224, format=vs.YUV420P8, matrix_s="709"
    )
    upscaled_metrics = metrics_func(upscaled_metrics)

    # replacing frames
    clip = core.akarin.Select(
        [upscaled, upscaled[1:] + upscaled[-1]],
        upscaled_metrics,
        "x.float_ssim 0.999 >",
    )

    clip = vs.core.resize.Bicubic(clip, format=vs.YUV420P8, matrix_s="709")
    return clip
