import itertools
import numpy as np
import vapoursynth as vs
from .GMFupSS_arch import Model_inference
import torch


# https://github.com/HolyWu/vs-rife/blob/master/vsrife/__init__.py
class GMFupSS:
    def __init__(self, partial_fp16=False):
        self.cache = False
        self.amount_input_img = 2

        torch.set_grad_enabled(False)
        torch.backends.cudnn.enabled = True
        torch.backends.cudnn.benchmark = True

        self.model = Model_inference(partial_fp16=partial_fp16)
        self.model.eval()

    def execute(self, I0, I1, timestep):
        with torch.inference_mode():
            middle = self.model(I0, I1, timestep).cpu()
        return middle
