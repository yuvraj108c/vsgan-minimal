import warnings
warnings.filterwarnings("ignore")
import sys
from inference_config import inference_clip

clip = inference_clip(config["video"], config["engine"])
clip.set_output()
