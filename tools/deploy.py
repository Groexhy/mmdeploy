import argparse
import logging
import os.path as osp
from functools import partial

import mmcv
import torch.multiprocessing as mp
from torch.multiprocessing import Process, set_start_method

from mmdeploy.apis import extract_model, inference_model, torch2onnx


def parse_args():
    parser = argparse.ArgumentParser(description='Export model to backend.')
    parser.add_argument('deploy_cfg', help='deploy config path')
    parser.add_argument('model_cfg', help='model config path')
    parser.add_argument('checkpoint', help='model checkpoint path')
    parser.add_argument(
        'img', help='image used to convert model and test model')
    parser.add_argument('--work-dir', help='the dir to save logs and models')
    parser.add_argument(
        '--device', help='device used for conversion', default='cpu')
    parser.add_argument(
        '--log-level',
        help='set log level',
        default='INFO',
        choices=list(logging._nameToLevel.keys()))
    parser.add_argument(
        '--show', action='store_true', help='Show detection outputs')
    args = parser.parse_args()

    return args


def target_wrapper(target, log_level, *args, **kwargs):
    logger = logging.getLogger()
    logger.level
    logger.setLevel(log_level)
    return target(*args, **kwargs)


def create_process(name, target, args, kwargs, ret_value=None):
    logging.info(f'{name} start.')
    log_level = logging.getLogger().level

    wrap_func = partial(target_wrapper, target, log_level)

    process = Process(target=wrap_func, args=args, kwargs=kwargs)
    process.start()
    process.join()

    if ret_value is not None:
        if ret_value.value != 0:
            logging.error(f'{name} failed.')
            exit()
        else:
            logging.info(f'{name} success.')


def main():
    args = parse_args()
    set_start_method('spawn')

    logger = logging.getLogger()
    logger.setLevel(args.log_level)

    deploy_cfg_path = args.deploy_cfg
    model_cfg_path = args.model_cfg
    checkpoint_path = args.checkpoint

    # load deploy_cfg
    deploy_cfg = mmcv.Config.fromfile(deploy_cfg_path)
    if not isinstance(deploy_cfg, (mmcv.Config, mmcv.ConfigDict)):
        raise TypeError('deploy_cfg must be a filename or Config object, '
                        f'but got {type(deploy_cfg)}')

    # create work_dir if not
    mmcv.mkdir_or_exist(osp.abspath(args.work_dir))

    ret_value = mp.Value('d', 0, lock=False)

    # convert onnx
    onnx_save_file = deploy_cfg['pytorch2onnx']['save_file']
    create_process(
        'torch2onnx',
        target=torch2onnx,
        args=(args.img, args.work_dir, onnx_save_file, deploy_cfg_path,
              model_cfg_path, checkpoint_path),
        kwargs=dict(device=args.device, ret_value=ret_value),
        ret_value=ret_value)

    # convert backend
    onnx_files = [osp.join(args.work_dir, onnx_save_file)]

    # split model
    apply_marks = deploy_cfg.get('apply_marks', False)
    if apply_marks:
        assert hasattr(deploy_cfg, 'split_params')
        split_params = deploy_cfg.get('split_params', None)

        origin_onnx_file = onnx_files[0]
        onnx_files = []
        for split_param in split_params:
            save_file = split_param['save_file']
            save_path = osp.join(args.work_dir, save_file)
            start = split_param['start']
            end = split_param['end']

            create_process(
                f'split model {save_file} with start: {start}, end: {end}',
                extract_model,
                args=(origin_onnx_file, start, end),
                kwargs=dict(save_file=save_path, ret_value=ret_value),
                ret_value=ret_value)

            onnx_files.append(save_path)

    backend_files = onnx_files
    # convert backend
    backend = deploy_cfg.get('backend', 'default')
    if backend == 'tensorrt':
        assert hasattr(deploy_cfg, 'tensorrt_params')
        tensorrt_params = deploy_cfg['tensorrt_params']
        model_params = tensorrt_params.get('model_params', [])
        assert len(model_params) == len(onnx_files)

        from mmdeploy.apis.tensorrt import onnx2tensorrt
        backend_files = []
        for model_id, model_param, onnx_path in zip(
                range(len(onnx_files)), model_params, onnx_files):
            onnx_name = osp.splitext(osp.split(onnx_path)[1])[0]
            save_file = model_param.get('save_file', onnx_name + '.engine')

            create_process(
                f'onnx2tensorrt of {onnx_path}',
                target=onnx2tensorrt,
                args=(args.work_dir, save_file, model_id, deploy_cfg_path,
                      onnx_path),
                kwargs=dict(device=args.device, ret_value=ret_value),
                ret_value=ret_value)

            backend_files.append(osp.join(args.work_dir, save_file))

    # check model outputs by visualization
    codebase = deploy_cfg['codebase']

    # visualize model of the backend
    create_process(
        f'visualize {backend} model',
        target=inference_model,
        args=(model_cfg_path, backend_files, args.img),
        kwargs=dict(
            codebase=codebase,
            backend=backend,
            device=args.device,
            output_file=f'output_{backend}.jpg',
            show_result=args.show,
            ret_value=ret_value),
        ret_value=ret_value)

    # visualize pytorch model
    create_process(
        'visualize pytorch model',
        target=inference_model,
        args=(model_cfg_path, [checkpoint_path], args.img),
        kwargs=dict(
            codebase=codebase,
            backend='pytorch',
            device=args.device,
            output_file='output_pytorch.jpg',
            show_result=args.show,
            ret_value=ret_value),
        ret_value=ret_value)

    logging.info('All process success.')


if __name__ == '__main__':
    main()