import sys
import os

sys.path.append("/workspace/tensorrt/")
import vapoursynth as vs
from src.scene_detect import scene_detect
from src.vfi_inference import vfi_frame_merger
from src.rife_trt import rife_trt

core = vs.core
vs_api_below4 = vs.__api_version__.api_major < 4
core.num_threads = 8

core.std.LoadPlugin(path="/usr/local/lib/libvstrt.so")


def metrics_func(clip):
    offs1 = core.std.BlankClip(clip, length=1) + clip[:-1]
    offs1 = core.std.CopyFrameProps(offs1, clip)
    return core.vmaf.Metric(clip, offs1, 2)


def inference_clip(video_path="", clip=None):
    interp_scale = 2

    clip = core.bs.VideoSource(source=video_path)

    # ssim
    clip_metric = vs.core.resize.Bicubic(
        clip, width=224, height=224, format=vs.YUV420P8, matrix_s="709"
    )
    clip_metric = metrics_func(clip_metric)

    clip = vs.core.resize.Bicubic(clip, format=vs.RGBH, matrix_in_s="709")

    # scene change
    clip_sc = scene_detect(
        clip,
        thresh=0.98,
        onnx_path="/workspace/tensorrt/sc_efficientnetv2b0+rife46_flow_1362_256_CHW_6ch_clamp_softmax_op17_fp16_sim.onnx",
        resolution=256,
    )

    # adjusting clip length
    clip_orig = vs.core.std.Interleave([clip] * interp_scale)
    clip_sc = vs.core.std.Interleave([clip_sc] * interp_scale)
    clip_metric = vs.core.std.Interleave([clip_metric] * interp_scale)

    # interpolation
    clip = rife_trt(
        clip,
        multi=interp_scale,
        scale=1.0,
        device_id=0,
        num_streams=2,
        engine_path="/workspace/tensorrt/rife414_ensembleTrue_op18_fp16_clamp_sim.engine",
    )

    # replacing frames
    clip = core.akarin.Select([clip, clip_orig], clip_metric, "x.float_ssim 0.999 >")
    clip = core.akarin.Select([clip, clip_orig], clip_sc, "x._SceneChangeNext 1 0 ?")

    clip = vs.core.resize.Bicubic(clip, format=vs.YUV420P8, matrix_s="709")
    return clip
