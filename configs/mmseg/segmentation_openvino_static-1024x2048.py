_base_ = ['./segmentation_static.py', '../_base_/backends/openvino.py']

onnx_config = dict(input_shape=(2048, 1024))
